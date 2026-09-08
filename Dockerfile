FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend/dist ./frontend/dist
ENV PYTHONPATH=/app/backend TCG_DATA_DIR=/app/data LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
RUN useradd --create-home --uid 10001 tcg && mkdir -p /app/data && chown tcg:tcg /app/data
USER tcg
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "tcg.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
