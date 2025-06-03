# localhost_ai_app.py

# # Block 1: Set up dependencies
# On a local machine, you would typically run this in your terminal inside a virtual environment to install the necessary packages.
# \!pip install flask langchain openai python-dotenv langchain_openai -q -q -q

# # Block 2: Import Libraries and Configure API Key

# import the dependencies
import os
from flask import Flask, request, jsonify
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- OpenAI API Key Configuration ---
# This relies on the OPENAI_API_KEY being set in your .env file
api_key = os.getenv("OPENAI_API_KEY")

# # Block 3: Define Prompt Template and Initialize LangChain with OpenAI

# Initialize the OpenAI LLM
# You can choose different models 
# The script will fail here if api_key is None (i.e., not found in .env)
# OPENAI MODEL REFERENCE - (https://platform.openai.com/docs/models)
llm = ChatOpenAI(openai_api_key=api_key, model_name="gpt-4o-mini")

# Define the prompt template
# This template instructs the AI and includes a placeholder for user input.
prompt_template_str = """
You are a helpful AI assistant. Answer the user's question clearly and concisely.

User Question: {user_input}

AI Response:
"""
prompt_template = ChatPromptTemplate.from_template(prompt_template_str)

print("LangChain components initialized.")

# Block 4: Set up Flask Application and Implement Chat Endpoint

# --- Set up Flask Application ---
app = Flask(__name__)
print("Flask application created.")

@app.route('/chat', methods=['POST'])
def chat_endpoint():
    try:
        data = request.get_json()
        if not data or 'user_input' not in data:
            return jsonify({"error": "No user_input provided in JSON payload."}), 400

        user_input = data['user_input']

        # Create the LangChain chain (Prompt + LLM)
        # LCEL (LangChain Expression Language) is used here
        chain = prompt_template | llm

        # Invoke the chain with the user's input
        ai_response_message = chain.invoke({"user_input": user_input})
        
        # The response from ChatOpenAI is an AIMessage object, access its content
        ai_response_content = ai_response_message.content

        return jsonify({"ai_response": ai_response_content})

    except Exception as e:
        # Log the error for debugging on the server side
        print(f"Error processing request: {e}") # Basic logging
        return jsonify({"error": "An error occurred while processing your request.", "details": str(e)}), 500

print("Flask /chat endpoint configured.")

# # Block 5: Run Locally (Start the Flask Server)

# Steps to run the Flask application:
# 1. Activate your virtual environment.
# 2. Enter "python3 localhost_ai_app.py" into your terminal.
# 3. In a new terminal, with your flask server running, enter "curl -X POST -H "Content-Type: application/json" -d '{"user_input":"Hello, AI! Whats the capital of Norway?"}' http://127.0.0.1:8000/chat"

# --- Run Flask Application ---
if __name__ == '__main__':
    if not api_key:
        print("--------------------------------------------------------------------")
        print("ERROR: OpenAI API key not found.")
        print("Please create a .env file in the same directory as this script with:")
        print("OPENAI_API_KEY=\"your_openai_api_key_here\"")
        print("--------------------------------------------------------------------")
    else:
        print("Starting Flask server...")
        # host='0.0.0.0' makes it accessible from your network, not just localhost
        # debug=True is useful for development, provides more error details
        app.run(host='0.0.0.0', port=8000, debug=True)