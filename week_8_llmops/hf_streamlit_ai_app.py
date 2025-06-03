# Block 2: Create the Streamlit Application File (app.py)
import streamlit as st
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate

# --- Page Configuration ---
st.set_page_config(
    page_title="AI Chat Assistant",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🤖 Streamlit AI Chat Assistant")
st.markdown("""
Welcome! Ask any question to the AI assistant. This application uses OpenAI's `gpt-4o-mini` model.
Enter your OpenAI API Key in the sidebar to begin.
""")

# --- API Key Handling ---
openai_api_key = None

# Attempt to get API key from st.secrets (for deployed apps)
try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
    if openai_api_key:
        st.sidebar.success("API key loaded from st.secrets!")
    else: # Handle case where secret exists but is empty
        st.sidebar.warning("OpenAI API Key found in st.secrets but it's empty. Please provide a valid key.")
except (KeyError, FileNotFoundError): # FileNotFoundError for local st.secrets.toml if used
    st.sidebar.info("OpenAI API Key not found in st.secrets. Please enter it below for this session.")

# Fallback to user input if not found in secrets or if secret was empty
if not openai_api_key:
    openai_api_key_input = st.sidebar.text_input(
        "Enter your OpenAI API Key:",
        type="password",
        key="api_key_input_sidebar",
        help="Your API key is used only for this session and not stored."
    )
    if openai_api_key_input:
        openai_api_key = openai_api_key_input

if not openai_api_key:
    st.warning("Please provide your OpenAI API Key in the sidebar to use the chat.")
    st.stop() # Stop execution if no API key is available

# --- LangChain Setup (Cached for efficiency) ---
@st.cache_resource # Caches the LLM and prompt template
def get_langchain_components(_api_key_for_cache): # Parameter ensures cache reacts to API key changes if necessary
    """Initializes and returns the LangChain LLM and prompt template."""
    llm = ChatOpenAI(openai_api_key=_api_key_for_cache, model_name="gpt-4o-mini")
    
    prompt_template_str = """
    You are a knowledgeable and friendly AI assistant.
    Your goal is to provide clear, concise, and helpful answers to the user's questions.
    If you don't know the answer to a specific question, it's better to say so rather than inventing one.

    User Question: {user_input}

    AI Response:
    """
    prompt = ChatPromptTemplate.from_template(prompt_template_str)
    return llm, prompt

try:
    llm, prompt_template = get_langchain_components(openai_api_key)
except Exception as e:
    st.error(f"Failed to initialize AI components. Error: {e}. Check your API key and model access.")
    st.stop()

# --- Initialize session state for storing chat messages ---
if "messages" not in st.session_state:
    st.session_state.messages = []

# --- Display existing chat messages ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- Chat Input and AI Response Logic ---
if user_query := st.chat_input("What would you like to ask?"):
    # Add user message to session state and display it
    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    # Get and display AI response
    with st.chat_message("assistant"):
        message_placeholder = st.empty() # For "Thinking..." message and then the actual response
        with st.spinner("AI is thinking..."):
            try:
                chain = prompt_template | llm
                ai_response_message = chain.invoke({"user_input": user_query})
                ai_response_content = ai_response_message.content
                
                message_placeholder.markdown(ai_response_content)
                # Add AI response to session state
                st.session_state.messages.append({"role": "assistant", "content": ai_response_content})

            except Exception as e:
                error_message = f"Sorry, I encountered an error: {str(e)}"
                message_placeholder.error(error_message)
                st.session_state.messages.append({"role": "assistant", "content": error_message})

# --- Sidebar Options ---
with st.sidebar:
    st.divider()
    if st.button("Clear Chat History", key="clear_chat"):
        st.session_state.messages = []
        st.success("Chat history cleared!")
        st.rerun() # Rerun to update the UI immediately

    st.markdown("---")
    st.subheader("About")
    st.info(
        "This is a Streamlit application demonstrating an AI chat interface "
        "using LangChain and OpenAI's gpt-4o-mini model."
    )


# --- Block 3: Run the Streamlit Application ---

# To start your streamlit app, run the following command in your terminal:

# streamlit run hf_streamlit_ai_app.py
# Make sure you have the required packages installed:
# pip install streamlit langchain openai langchain_openai -q