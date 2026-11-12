FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir moomoo-api || echo 'moomoo-api not installed: moomoo prices will stay off'
COPY VERSION ./VERSION
COPY bot ./bot
COPY scripts ./scripts
CMD ["python", "-m", "bot.main"]
