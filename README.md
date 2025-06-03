
## Key Components

### Data
- **`data/`**: Contains patient encounter data in CSV format for analysis and embedding.

### Final Project
- **`final_project/`**: Includes Python scripts and Jupyter notebooks for patient data processing and visualization.
  - **Streamlit Apps**: Interactive apps for patient lookup and RAG-based question answering.

### Week 7 RAG
- **`week_7_rag/`**: Contains text-based resources, including the Project Gutenberg version of *Moby Dick*.

### Week 8 LLMOps
- **`week_8_llmops/`**: Implements AI applications using Gradio and Streamlit frameworks.

## Installation
1. Clone the repository:
   ```sh
   git clone <repository-url>
   cd <repository-folder>
   ```

## Install dependencies:
pip install -r requirements.txt

## Usage
* Run final project Streamlit apps:
streamlit run final_project/streamlit/streamlit_app.py
streamlit run final_project/streamlit/streamlit_app_with_rag.py

* Run Gradio apps:
python week_8_llmops/hf_gradio_ai_app.py

## License
This repository includes content from Project Gutenberg, which is in the public domain. Refer to week_7_rag/Project_Gutenberg_Moby Dick_Secret.txt for licensing details.