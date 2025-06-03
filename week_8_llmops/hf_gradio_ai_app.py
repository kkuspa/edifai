# gradio_ai_chatbot_dotenv.py
#
# To run this script:
# 1. Create a .env file in the same directory with your OPENAI_API_KEY.
#    Example .env file content:
#    OPENAI_API_KEY="sk-yourActualOpenAIapiKeyGoesHere"
# 2. Install the required packages:
#    pip install gradio langchain openai langchain_openai python-dotenv
# 3. Run the script from your terminal:
#    python gradio_ai_chatbot_dotenv.py
#
# The script will output a local URL and potentially a public Gradio link.

import gradio as gr
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
import os
from dotenv import load_dotenv

# --- Load environment variables from .env file ---
load_dotenv()

# --- Global variables and Initial Setup ---
OPENAI_API_KEY_GLOBAL = os.getenv("OPENAI_API_KEY")
LANGCHAIN_LLM = None
LANGCHAIN_PROMPT_TEMPLATE = None
INITIAL_AI_SETUP_MESSAGE = "" # To store status/error from initial setup

def initialize_ai_components():
    """
    Initializes LangChain components (LLM and prompt template) using the API key
    from environment variables. Updates global variables and sets a status message.
    """
    global LANGCHAIN_LLM, LANGCHAIN_PROMPT_TEMPLATE, OPENAI_API_KEY_GLOBAL, INITIAL_AI_SETUP_MESSAGE
    
    if not OPENAI_API_KEY_GLOBAL:
        INITIAL_AI_SETUP_MESSAGE = "<p style='color:red; font-weight:bold;'>ERROR: OpenAI API Key not found. Please ensure it's in your .env file or environment variables.</p>"
        print("ERROR: OpenAI API Key not found. Make sure it's in your .env file or environment.")
        return False # Indicate failure
    
    try:
        # Initialize the LangChain LLM (OpenAI model)
        LANGCHAIN_LLM = ChatOpenAI(openai_api_key=OPENAI_API_KEY_GLOBAL, model_name="gpt-4o-mini")
        
        # Define the prompt template for the LLM
        prompt_template_str = """
        You are a helpful, friendly, and insightful AI assistant.
        Answer the user's question clearly, concisely, and in a conversational tone.
        If you don't know the answer or a question is ambiguous, ask for clarification or state that you don't know.

        User Question: {user_input}

        AI Response:
        """
        LANGCHAIN_PROMPT_TEMPLATE = ChatPromptTemplate.from_template(prompt_template_str)
        
        INITIAL_AI_SETUP_MESSAGE = "<p style='color:green; font-weight:bold;'>AI Components Initialized Successfully! Ready to chat.</p>"
        print("AI Components Initialized Successfully!")
        return True # Indicate success
    except Exception as e:
        INITIAL_AI_SETUP_MESSAGE = f"<p style='color:red; font-weight:bold;'>ERROR: Failed to initialize AI components. Error: {str(e)}. Please check your API key and model access.</p>"
        LANGCHAIN_LLM = None
        LANGCHAIN_PROMPT_TEMPLATE = None
        print(f"ERROR: Failed to initialize AI components: {str(e)}")
        return False # Indicate failure

# --- Attempt to initialize AI components when the script loads ---
AI_INITIALIZED_SUCCESSFULLY = initialize_ai_components()

def ai_chat_response_function(user_message, chat_history):
    """
    This is the core function called by Gradio's ChatInterface.
    It takes the user's message and the chat history, and returns the AI's response string.
    """
    if not AI_INITIALIZED_SUCCESSFULLY or not LANGCHAIN_LLM or not LANGCHAIN_PROMPT_TEMPLATE:
        # Use the globally set error message from initialization
        # Clean up HTML for plain error string if needed, or pass raw if Markdown supports it
        error_msg_text = INITIAL_AI_SETUP_MESSAGE.replace("<p style='color:red; font-weight:bold;'>", "").replace("</p>", "")
        return f"ERROR: AI is not ready. Status: {error_msg_text}"

    # Proceed with generating response if components are ready
    try:
        # Create the LangChain chain (Prompt + LLM)
        chain = LANGCHAIN_PROMPT_TEMPLATE | LANGCHAIN_LLM
        
        # Invoke the chain with the user's input
        ai_response = chain.invoke({"user_input": user_message})
        
        # Return the content of the AI's response
        return ai_response.content
    except Exception as e:
        print(f"Error during LangChain invocation: {e}") # Log for server-side debugging
        return f"Sorry, an error occurred while trying to get a response: {str(e)}"

# --- Gradio Interface Definition using gr.Blocks for layout control ---
with gr.Blocks(theme=gr.themes.Soft(primary_hue=gr.themes.colors.blue, secondary_hue=gr.themes.colors.sky), title="AI Chatbot (Gradio)") as gradio_app:
    gr.Markdown(
        """
        # 🤖 AI Chatbot with Gradio, LangChain & OpenAI
        Powered by OpenAI's `gpt-4o-mini` model.
        OpenAI API Key is loaded from your `.env` file.
        """
    )

    # Display the initial AI setup status
    gr.Markdown(INITIAL_AI_SETUP_MESSAGE)
    
    gr.Markdown("---") # Visual separator
    gr.Markdown("## Chat Interface")

    # Gradio ChatInterface for the main chat functionality
    chat_interface_component = gr.ChatInterface(
        fn=ai_chat_response_function, # The function that handles chat logic
        chatbot=gr.Chatbot(
            height=550,
            show_label=False,
            placeholder="AI's responses will appear here." if AI_INITIALIZED_SUCCESSFULLY else "AI is not available. Check setup status above.",
            avatar_images=("https://raw.githubusercontent.com/svgmoji/svgmoji/main/packages/svgmoji__openmoji/svg/1F468-1F3FB-200D-1F9B0.svg", "https://raw.githubusercontent.com/gradio-app/gradio/main/gradio/icons/huggingface-logo.svg"),
            type='messages'
        ),
        textbox=gr.Textbox(
            placeholder="Type your message here and press Enter...",
            show_label=False,
            scale=7,
            # Disable textbox if AI did not initialize successfully
            interactive=AI_INITIALIZED_SUCCESSFULLY
        ),
        submit_btn="➡️ Send" if AI_INITIALIZED_SUCCESSFULLY else None, # Hide button if not ready
        examples=[
            "What is Paris, France known for?",
            "Explain the concept of a Large Language Model (LLM) simply.",
            "Can you give me a basic recipe for brownies?",
            "Tell me an interesting fact about sunflowers."
        ] if AI_INITIALIZED_SUCCESSFULLY else None, # Only show examples if AI is ready
        title=None,
        autofocus=True
    )
    
    # If AI initialization failed, you might want to make the ChatInterface non-interactive.
    # One way is to conditionally enable/disable components or hide buttons as done above.
    if not AI_INITIALIZED_SUCCESSFULLY:
        # Further disable parts of the chat interface if needed, though ChatInterface
        # doesn't have a simple 'interactive=False' for the whole thing.
        # Hiding buttons and disabling textbox is a good start.
        # The error message in `ai_chat_response_function` will also prevent interaction.
        pass


# --- Main execution block to launch the Gradio app ---
if __name__ == '__main__':
    print("Attempting to launch Gradio App...")
    if not OPENAI_API_KEY_GLOBAL:
        print("WARNING: OpenAI API Key was not found in environment variables or .env file.")
        print("The application UI will launch, but AI functionality will be disabled.")
        print("Please create a .env file with your OPENAI_API_KEY.")
    
    gradio_app.launch(share=True, debug=True)