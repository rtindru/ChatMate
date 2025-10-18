# Set the Env Variables
export LICHESS_TOKEN=xxxxxx
export STOCKFISH_PATH=/path/to/stockfish

# Run a local server
uvicorn server:app --reload --log-level debug --use-colors --host 0.0.0.0 --port 8000

# Expose your local server on the internet
ngrok http 8000
(install ngrok if needed)

# Replace the server_url in OpenAPISpec.yaml
servers:
  - url: https://your-unique-url.ngrok-free.app

# Create a Custom GPT Agent using the YAML
https://help.openai.com/en/articles/8554397-creating-a-gpt
