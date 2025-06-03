import streamlit as st
from langchain_openai.chat_models import ChatOpenAI
#from langchain_openai.embeddings import OpenAIEmbeddings
#from langchain.vectorstores import Chroma
import os
from dotenv import load_dotenv


st.title("Patient Lookup App")

# Load environment variables from .env file
load_dotenv()

# --- OpenAI API Key Configuration ---
# This relies on the OPENAI_API_KEY being set in your .env file
openai_api_key = os.getenv("OPENAI_API_KEY")
# st.sidebar.text_input("OpenAI API Key", type="password")


def generate_response(input_text):
    model = ChatOpenAI(temperature=0.7, api_key=openai_api_key)
    st.info(model.invoke(input_text).content, icon="🤖")


with st.form("my_form"):
    text = st.text_area(
        "Enter text:",
        "What would you like to know about your patients today?",
    )
    submitted = st.form_submit_button("Submit")
    if not openai_api_key.startswith("sk-"):
        st.warning("Please enter your OpenAI API key!", icon="⚠")
    if submitted:
        generate_response(text)