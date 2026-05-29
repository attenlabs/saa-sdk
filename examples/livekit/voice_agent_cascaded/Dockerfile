# minimal image for the SAA-gated cascaded agent
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# pre-download plugin model files (Silero VAD weights) into the image
RUN python src/agent.py download-files || true

# LiveKit Agents worker — joins dispatched rooms
CMD ["python", "src/agent.py", "start"]
