FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
COPY app.py ./

RUN uv sync --python 3.12

RUN mkdir -p logs

CMD ["uv", "run", "uvicorn", "review_manager_mon.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
