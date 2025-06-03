import streamlit as st
from langchain_openai.chat_models import ChatOpenAI
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma # Updated import for Chroma
from langchain.document_loaders.csv_loader import CSVLoader # For loading CSV
from langchain.prompts import ChatPromptTemplate
from langchain.schema.runnable import RunnablePassthrough
from langchain.schema.output_parser import StrOutputParser
# from langchain.text_splitter import RecursiveCharacterTextSplitter # Optional: if your CSV column text is long

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# --- Configuration ---
# IMPORTANT: Replace with your actual OpenAI API key, CSV file path, and the column name to embed.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CSV_FILE_PATH = "data/patient_encounters1_notes_head.csv"  # Replace with your CSV file path
COLUMN_TO_EMBED = "CLINICAL_NOTES" # Replace with the relevant column name in your CSV

st.title("⚕️ Patient Lookup App")
st.subheader("Now with Retrieval Augmented Generation (RAG)!")

# --- Helper function to format retrieved documents ---
def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# --- Caching function to load, embed, and store data in ChromaDB ---
@st.cache_resource(show_spinner="Loading and embedding patient data...")
def load_and_embed_data(_csv_file_path, _column_to_embed, _openai_api_key):
    try:
        loader = CSVLoader(file_path=_csv_file_path, source_column=_column_to_embed, encoding='utf-8') # Specify encoding if needed
        documents = loader.load()

        # Optional: If the text in your CSV column is very long, you might want to split it.
        # text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        # texts = text_splitter.split_documents(documents)
        # if not texts: # If splitting results in no texts, use original documents
        #     texts = documents

        if not documents:
            st.error(f"No documents found in '{_csv_file_path}' from column '{_column_to_embed}'. Please check the file and column name.")
            return None

        embeddings_model = OpenAIEmbeddings(api_key=_openai_api_key)
        # Ensure documents have non-empty page_content
        valid_documents = [doc for doc in documents if doc.page_content and doc.page_content.strip()]
        if not valid_documents:
            st.error(f"All documents loaded from '{_column_to_embed}' have empty content. Cannot create embeddings.")
            return None

        vectorstore = Chroma.from_documents(
            documents=valid_documents, # Use valid_documents
            embedding=embeddings_model,
            # persist_directory="./chroma_db" # Optional: for persisting the DB
        )
        return vectorstore
    except Exception as e:
        st.error(f"Error loading or embedding data: {e}")
        return None

# --- Load vectorstore (will be cached after first run) ---
if CSV_FILE_PATH != "YOUR_FILE.csv" and COLUMN_TO_EMBED != "your_column_name_for_embedding":
    vectorstore = load_and_embed_data(CSV_FILE_PATH, COLUMN_TO_EMBED, OPENAI_API_KEY)
else:
    vectorstore = None

# --- RAG Enhanced Response Generation ---
def generate_rag_response(input_text, _vectorstore, _openai_api_key):
    if _vectorstore is None:
        st.error("Vector store not initialized. Please check CSV path and column configuration.")
        return "Error: Vector store not available."

    llm = ChatOpenAI(temperature=0.7, api_key=_openai_api_key)
    retriever = _vectorstore.as_retriever(search_kwargs={"k": 3}) # Retrieve top 3 relevant documents

    # Define the RAG prompt template
    prompt_template = """You are an AI assistant helping with patient information.
    Based on the following retrieved context from patient records, please answer the question.
    If the context doesn't contain the answer, state that the information is not available in the retrieved records.
    Keep your answer concise and directly related to the question.

    Context:
    {context}

    Question: {question}

    Answer:"""
    
    prompt = ChatPromptTemplate.from_template(prompt_template)

    # Create the RAG chain
    rag_chain = (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    try:
        response = rag_chain.invoke(input_text)
        st.info(response, icon="🤖")
    except Exception as e:
        st.error(f"Error generating response: {e}")


# --- Streamlit Form ---
with st.form("my_form"):
    text = st.text_area(
        "Enter your question about patients:",
        "e.g., What is the latest update for patient ID 12345?",
    )
    submitted = st.form_submit_button("Submit")

    if submitted:
        if not OPENAI_API_KEY or not OPENAI_API_KEY.startswith("sk-"):
            st.warning("Please enter a valid OpenAI API key in the code!", icon="⚠️")
        elif vectorstore is None:
             st.warning("Data could not be loaded. Please check configurations and file paths.", icon="📁")
        elif not text.strip():
            st.warning("Please enter a question.", icon="❓")
        else:
            with st.spinner("Finding relevant patient information and generating response..."):
                generate_rag_response(text, vectorstore, OPENAI_API_KEY)

st.sidebar.header("About")
st.sidebar.info(
    "This app demonstrates Retrieval Augmented Generation (RAG) "
    "to answer questions about patients using information from your supplied data source. "
    "It uses OpenAI for embeddings and language modeling, and ChromaDB for vector storage."
)
st.sidebar.markdown(
    "Which features will you add to your app next? "
    "Some ideas include:\n"
    "1. Support for multiple data sources\n"
    "2. LLM security and privacy features\n"
    "2. Enhanced error handling and logging\n"
    "3. Data visualization options\n"
    "4. Integration with other AI models or APIs\n"
    "5. New UI themes and layouts\n"
    "6. Real-time data updates\n"
    "7. Interactive LLM parameters\n"
    "8. Prompt engineering for better responses\n"
)