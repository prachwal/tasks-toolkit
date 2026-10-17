FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
ENV VIBE_TASKS_ROOT=/data
VOLUME ["/data"]
ENTRYPOINT ["tasks-mcp"]
