#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
project_example.py
==================

Reference implementation for **Objective 1 (Encounter-Type Classifier)**,
**Objective 2 (Future-Trend Forecaster)**, *plus* the added
**zero-shot/LLM baseline** so students can compare a prompt-only approach
against their fine-tuned DistilBERT model.

Run once you have the CSVs in `data/` and your OpenAI key in the
environment.  Plots land in `plots/`.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# 0.  Imports and global configuration
# --------------------------------------------------------------------------- #
# import datetime as dt
import warnings
# import os
from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import ConfusionMatrixDisplay, classification_report
from sklearn.model_selection import train_test_split
from statsmodels.tools.sm_exceptions import ConvergenceWarning # Corrected import
from statsmodels.tsa.statespace.sarimax import SARIMAX
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

# --- LangChain bits for zero-shot classification --------------------------- #
from langchain.chat_models import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema import StrOutputParser

warnings.filterwarnings("ignore", category=ConvergenceWarning)
# Also filter the specific statsmodels ValueWarning if it persists after freq setting, though ideally it shouldn't
warnings.filterwarnings("ignore", message="A date index has been provided, but it has no associated frequency information", category=UserWarning) # Or ValueWarning depending on statsmodels version
warnings.filterwarnings("ignore", message="No supported index is available. Prediction results will be given with an integer index beginning at `start`.", category=UserWarning) # Or ValueWarning

DATA_DIR = Path("data")
PLOTS_DIR = Path("plots")
PLOTS_DIR.mkdir(exist_ok=True)

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MODEL_NAME = "distilbert-base-uncased"
SEED = 71
VAL_SUBSET = 500  # set -1 to evaluate the entire validation split for zero-shot

np.random.seed(SEED)
torch.manual_seed(SEED)


# --------------------------------------------------------------------------- #
# 1.  Objective 1  —  Encounter-Type Classifier (fine-tuned model)
# --------------------------------------------------------------------------- #

def load_classifier_df() -> pd.DataFrame:
    """Load and prepare dataframe for classification."""
    df = pd.read_csv(DATA_DIR / "final.csv")
    text_cols = [
        "DESCRIPTION_OBSERVATIONS",
        "DESCRIPTION_CONDITIONS",
        "DESCRIPTION_PROCEDURES",
        "DESCRIPTION_MEDICATIONS",
    ]
    df["text"] = df[text_cols].fillna("").agg(" ".join, axis=1)
    df = df[df["text"].str.strip().astype(bool)].reset_index(drop=True)
    return df


def make_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Patient-level split to avoid leakage."""
    train_ids, val_ids = train_test_split(
        df["PATIENT_ID"].unique(), test_size=0.2, random_state=SEED
    )
    return (
        df[df["PATIENT_ID"].isin(train_ids)].reset_index(drop=True),
        df[df["PATIENT_ID"].isin(val_ids)].reset_index(drop=True),
    )


class EncDataset(torch.utils.data.Dataset):
    """PyTorch dataset for Transformers Trainer."""

    def __init__(
        self, df: pd.DataFrame, tokenizer: AutoTokenizer, label2id: Dict[str, int]
    ) -> None:
        self.labels = df["ENCOUNTERCLASS"].map(label2id).tolist()
        self.encodings = tokenizer(
            df["text"].tolist(), padding="max_length", truncation=True, max_length=256
        )

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx: int):
        item = {k: torch.tensor(v[idx]) for k, v in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[idx])
        return item


def train_and_eval_classifier() -> tuple[float, Dict[str, int], Dict[int, str], pd.DataFrame]:
    """Finetune DistilBERT, plot metrics, return macro-F1 and label maps."""
    df = load_classifier_df()
    train_df, val_df = make_splits(df)

    label_counts = df["ENCOUNTERCLASS"].value_counts()
    label2id = {lbl: i for i, lbl in enumerate(label_counts.index)}
    id2label = {i: lbl for lbl, i in label2id.items()}

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME,
        num_labels=len(label2id),
        id2label=id2label,
        label2id=label2id,
    ).to(DEVICE)

    train_ds = EncDataset(train_df, tokenizer, label2id)
    val_ds = EncDataset(val_df, tokenizer, label2id)

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = np.argmax(logits, axis=-1)
        rep = classification_report(labels, preds, output_dict=True, zero_division=0)
        return {"macro_f1": rep["macro avg"]["f1-score"]}

    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir="checkpoints/enc_cls",
            per_device_train_batch_size=16,
            per_device_eval_batch_size=32,
            num_train_epochs=3,
            # evaluation_strategy="epoch", # Removed due to error: "unexpected keyword argument 'evaluation_strategy'"
                                          # This implies an older version of transformers or an environment issue.
                                          # Evaluation will be done via trainer.predict() after training.
            # save_strategy="epoch", # Removed, as it typically aligns with evaluation_strategy
            # load_best_model_at_end=True, # Removed, requires evaluation during training to identify best model
            # metric_for_best_model="macro_f1", # Removed
            seed=SEED,
            logging_steps=100, # Logs training loss; does not imply evaluation frequency here.
            # Note: without evaluation_strategy, the model saved by default (if save_steps or save_total_limit is used)
            # or at the end will be the last state, not necessarily the best performing on validation.
        ),
        train_dataset=train_ds,
        eval_dataset=val_ds, # eval_dataset is still useful for explicit .evaluate() or .predict() calls
        compute_metrics=compute_metrics,
    )

    # trainer.train()

    # read the checkpoint from the last training run
    trainer.model = AutoModelForSequenceClassification.from_pretrained(
        "checkpoints/enc_cls/checkpoint-3000", # Replace XXXX with the actual checkpoint number
        num_labels=len(label2id),
        id2label=id2label,
        label2id=label2id,
    ).to(DEVICE)

    # Confusion matrix for fine-tuned model (evaluated on the final model state)
    preds = trainer.predict(val_ds)
    y_true = preds.label_ids
    y_pred = np.argmax(preds.predictions, axis=1)
    
    # Ensure display_labels correspond to the unique set of true and predicted labels
    # or are derived from id2label in the correct order if all labels are covered.
    # Using list(id2label.values()) assumes that all integer labels from 0 to N-1 exist and id2label is sorted by key.
    cm_display_labels = [id2label[i] for i in sorted(id2label.keys())]

    ConfusionMatrixDisplay.from_predictions(
        y_true, y_pred, display_labels=cm_display_labels, xticks_rotation=45
    )
    plt.title("Fine-tuned DistilBERT Confusion Matrix (Final Model)")
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "cls_confusion_finetune.png", dpi=300)
    plt.close()

    macro_f1 = classification_report(
        y_true, y_pred, labels=sorted(list(id2label.keys())), target_names=cm_display_labels, output_dict=True, zero_division=0
    )["macro avg"]["f1-score"]


    return macro_f1, label2id, id2label, val_df


# --------------------------------------------------------------------------- #
# 1b.  Zero-shot baseline via LangChain + OpenAI
# --------------------------------------------------------------------------- #

def run_zero_shot(val_df: pd.DataFrame, label2id: Dict[str, int]) -> float:
    """Classify validation set via an LLM prompt; return macro-F1."""
    labels_text_list = list(label2id.keys())
    label_str_for_prompt = ", ".join(labels_text_list)

    system_msg = (
        "You are a medical coding assistant. "
        "Select the best fitting category from the following list: {labels}. "
        "Return **only** the category word."
    )

    prompt = ChatPromptTemplate.from_messages(
        [("system", system_msg), ("human", "{text}")]
    )
    # Ensure OPENAI_API_KEY is in the environment
    #openai_api_key = os.getenv("OPENAI_API_KEY")
    llm = ChatOpenAI(model_name="gpt-3.5-turbo-0125", temperature=0)
    chain = prompt | llm | StrOutputParser()

    df_eval = val_df.sample(n=VAL_SUBSET, random_state=SEED) if VAL_SUBSET > 0 and VAL_SUBSET < len(val_df) else val_df

    preds_llm = []
    for text in df_eval["text"]:
        try:
            pred = chain.invoke({"labels": label_str_for_prompt, "text": text})
        except Exception as e:
            print(f"LLM invocation failed for a sample: {e}")
            pred = "unknown_llm_output" # Use a distinct placeholder for failed LLM calls
        preds_llm.append(pred) # Keep raw output for now

    # Process predictions: strip, lowercase, and map to IDs
    # Create a mapping for "unknown_llm_output" to a special ID if it occurs
    # and handle other LLM outputs that don't map to known labels.
    
    y_pred_str_processed = [p.strip().lower() for p in preds_llm]
    
    # Create a temporary mapping for label2id including a potential "unknown" class from LLM
    # This avoids modifying the original label2id which is tied to the fine-tuned model's classes
    temp_label2id = label2id.copy()
    known_ids = set(temp_label2id.values())
    
    # Determine a placeholder ID for labels not in known set or for explicit "unknown_llm_output"
    # Ensure this ID doesn't clash with existing label IDs
    next_available_id = max(known_ids) + 1 if known_ids else 0
    unknown_class_label_text = "unknown (from LLM)" # Text representation for this new class
    
    y_pred_mapped_ids = []
    #generated_unknown_class = False
    for p_str in y_pred_str_processed:
        if p_str == "unknown_llm_output": # Failed LLM call
            if unknown_class_label_text not in temp_label2id:
                temp_label2id[unknown_class_label_text] = next_available_id
                next_available_id +=1
            y_pred_mapped_ids.append(temp_label2id[unknown_class_label_text])
            #generated_unknown_class = True
        elif p_str in temp_label2id:
            y_pred_mapped_ids.append(temp_label2id[p_str])
        else: # LLM output is a string not in known labels and not a failed call
            if unknown_class_label_text not in temp_label2id: # Group all unmappable under one "unknown"
                 temp_label2id[unknown_class_label_text] = next_available_id
                 next_available_id +=1
            y_pred_mapped_ids.append(temp_label2id[unknown_class_label_text])
            #generated_unknown_class = True

    y_true_ids = df_eval["ENCOUNTERCLASS"].map(label2id).tolist() # True labels use original label2id

    # Create display names for classification report and confusion matrix
    # These names should correspond to the IDs present in y_true_ids and y_pred_mapped_ids
    
    # Build id2label for all potentially present labels (original + LLM-generated unknown)
    temp_id2label = {id_val: str_label for str_label, id_val in temp_label2id.items()}
    
    all_present_ids = sorted(list(set(y_true_ids + y_pred_mapped_ids)))
    final_display_labels = [temp_id2label.get(id_val, f"error_id_{id_val}") for id_val in all_present_ids]

    rep = classification_report(
        y_true_ids, y_pred_mapped_ids, 
        labels=all_present_ids, 
        target_names=final_display_labels, 
        output_dict=True, 
        zero_division=0
    )
    macro_f1 = rep["macro avg"]["f1-score"]

    ConfusionMatrixDisplay.from_predictions(
        y_true_ids, y_pred_mapped_ids, display_labels=final_display_labels, xticks_rotation=45
    )
    plt.title("Zero-shot LLM Confusion Matrix")
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "cls_confusion_zeroshot.png", dpi=300)
    plt.close()

    return macro_f1


# --------------------------------------------------------------------------- #
# 2.  Objective 2  —  Future-Trend Forecaster
# --------------------------------------------------------------------------- #

def load_timeseries_df() -> pd.DataFrame:
    """Load, parse dates, and aggregate data weekly for time series forecasting."""
    df = pd.read_csv(DATA_DIR / "final.csv", parse_dates=["START"])
    # Create a PeriodIndex for weekly aggregation (ISO weeks start on Monday)
    df["week_period"] = df["START"].dt.to_period("W")
    
    # Group by the Period object first to ensure correct aggregation boundaries
    grouped = df.groupby(["week_period", "ENCOUNTERCLASS"]).size().unstack(fill_value=0)
    
    # Convert PeriodIndex to DatetimeIndex (start of the period) and set frequency
    # 'W-MON' signifies that the timestamp represents the start of an ISO week (Monday)
    if not grouped.empty:
        grouped.index = grouped.index.to_timestamp(freq='W-MON')
    
    grouped = grouped.sort_index()
    return grouped


def naive_forecast(series: pd.Series, horizon: int) -> np.ndarray:
    """
    Generates a seasonal naive forecast.
    If series has >= 52 data points, it uses values from 52 weeks ago.
    Otherwise, it falls back to repeating the last known value.
    Returns NaNs if series is empty or too short for fallback.
    """
    if len(series) >= 52:
        # Use values from 52 weeks ago, for 'horizon' length
        return series.iloc[-52 : -52 + horizon].values
    elif len(series) >= 1:
        return np.full(horizon, series.iloc[-1])
    else:
        return np.full(horizon, np.nan)


def sarimax_forecast(series: pd.Series, horizon: int) -> np.ndarray:
    """Fits a SARIMAX model and forecasts 'horizon' steps ahead."""
    # SARIMAX needs a minimum number of observations, especially with seasonality.
    # For seasonal_order=(..., 52), it needs more than 52 data points.
    # Rule of thumb: at least 2 full seasons + order, e.g., 2*52 + p + q + P + Q
    # A simpler check here:
    min_obs_sarimax = 52 + 7 # Heuristic: one full season plus a bit more for estimation
    if len(series) < min_obs_sarimax:
        # print(f"Warning: Series length {len(series)} is too short for reliable SARIMAX (min ~{min_obs_sarimax}). Returning NaNs.")
        return np.full(horizon, np.nan)

    # Ensure index has frequency for SARIMAX if not already set (though load_timeseries_df should handle it)
    current_series = series.copy()
    if current_series.index.freq is None and isinstance(current_series.index, pd.DatetimeIndex):
        # Attempt to infer or set if 'load_timeseries_df' somehow missed it or series was altered
        inferred_freq = pd.infer_freq(current_series.index)
        if inferred_freq:
            current_series = current_series.asfreq(inferred_freq)
        else: # Fallback if inference fails, though 'W-MON' is expected
            # print(f"Warning: SARIMAX input series for {series.name} has no freq. Attempting to set 'W-MON'.")
            try:
                current_series = current_series.asfreq('W-MON') # Assuming weekly, Monday-starting
            except ValueError:
                # print(f"Error: Could not set frequency for {series.name}. Proceeding without explicit freq.")
                pass # Proceed, statsmodels might still handle it or warn.

    model = SARIMAX(
        current_series, order=(1, 0, 0), seasonal_order=(1, 1, 0, 52),
        enforce_stationarity=False, enforce_invertibility=False,
        initialization='approximate_diffuse'
    ).fit(disp=False)
    return model.forecast(horizon).values


def forecast_counts(horizon: int = 8) -> None:
    """Forecasts encounter counts using naive and SARIMAX models and plots MAE."""
    weekly_df = load_timeseries_df()
    mae: Dict[str, List[float]] = {"naive": [], "sarimax": []}

    if weekly_df.empty:
        print("Forecasting: Loaded timeseries data is empty. Skipping.")
        return

    for col_name in weekly_df.columns:
        series = weekly_df[col_name]

        # Min length for train_series to support seasonal naive: 52
        # Min length for series overall: 52 + horizon
        if len(series) < 52 + horizon :
            # print(f"Skipping '{col_name}': series too short (length {len(series)}) for forecast horizon {horizon} and seasonality 52.")
            continue

        train_series = series.iloc[:-horizon]
        test_series_actual = series.iloc[-horizon:].values

        # Naive forecast
        naive_preds = naive_forecast(train_series, horizon)
        if not np.all(np.isnan(naive_preds)): # Check if not all predictions are NaN
             mae["naive"].append(np.nanmean(np.abs(naive_preds - test_series_actual))) # Use nanmean for robustness
        # else:
            # print(f"Naive forecast for '{col_name}' resulted in all NaNs. MAE not calculated.")

        # SARIMAX forecast
        try:
            sarimax_preds = sarimax_forecast(train_series, horizon)
            if not np.all(np.isnan(sarimax_preds)): # Check if not all predictions are NaN
                mae["sarimax"].append(np.nanmean(np.abs(sarimax_preds - test_series_actual))) # Use nanmean
            # else:
                # print(f"SARIMAX forecast for '{col_name}' resulted in all NaNs. MAE not calculated.")
        except Exception as e:
            print(f"SARIMAX forecast failed for '{col_name}': {e}. Skipping this model for this series.")

    avg_mae = {
        k: np.nanmean(v) if v else np.nan for k, v in mae.items() # Ensure np.nanmean for lists that might contain NaNs from failed forecasts
    }
    
    valid_avg_mae = {k: v for k, v in avg_mae.items() if not np.isnan(v)}

    if not valid_avg_mae:
        print("Forecasting: No valid MAE results to plot (all forecasts might have failed or produced NaNs).")
        return

    plt.figure()
    plt.bar(valid_avg_mae.keys(), valid_avg_mae.values())
    plt.ylabel("MAE (counts)")
    plt.title("Forecasting – average MAE across encounter classes")
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "forecast_mae.png", dpi=300)
    plt.close()


# --------------------------------------------------------------------------- #
# 3.  Main driver
# --------------------------------------------------------------------------- #

def main():
    print("Objective 1: Encounter-Type Classifier")
    print("Training DistilBERT classifier…")
    # Initialize to ensure variables exist if try block fails early
    macro_f1_ft, macro_f1_zs = np.nan, np.nan 
    #classification_successful = False

    try:
        macro_f1_ft, label2id, id2label, val_df = train_and_eval_classifier()
        print(f"  Fine-tuned DistilBERT Macro F1: {macro_f1_ft:.3f}")
        # classification_successful = True

        print("\nRunning zero-shot baseline via LLM… (subset size:", VAL_SUBSET if VAL_SUBSET > 0 else "all", ")")
        macro_f1_zs = run_zero_shot(val_df, label2id)
        print(f"  Zero-shot LLM Macro F1: {macro_f1_zs:.3f}")

        plt.figure() 
        plt.bar(["Fine-tuned", "Zero-shot"], [macro_f1_ft, macro_f1_zs])
        plt.ylabel("Macro F1 Score")
        plt.title("Encounter Classifier: Fine-tuned vs Zero-shot Performance")
        plt.ylim(0, 1)
        plt.tight_layout()
        plt.savefig(PLOTS_DIR / "cls_f1_comparison.png", dpi=300)
        plt.close()
        print(f"\nClassification F1 comparison plot saved to {PLOTS_DIR / 'cls_f1_comparison.png'}")

    except FileNotFoundError as e:
        print(f"ERROR in Classification: {e}. Ensure '{DATA_DIR / 'final.csv'}' exists and is readable.")
    except Exception as e:
        print(f"An unexpected error occurred during classification: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for debugging unexpected errors


    # print("\nObjective 2: Future-Trend Forecaster")
    # print("Forecasting encounter counts…")
    # try:
    #     forecast_counts()
    #     # Message about plot saving is now inside forecast_counts if successful
    # except FileNotFoundError as e:
    #     print(f"ERROR in Forecasting: {e}. Ensure '{DATA_DIR / 'final.csv'}' exists and is readable.")
    # except Exception as e:
    #     print(f"An unexpected error occurred during forecasting: {e}")
    #     import traceback
    #     traceback.print_exc()

    print(f"\nScript finished. All plots attempted to be saved to directory: {PLOTS_DIR.resolve()}")


if __name__ == "__main__":
    main()