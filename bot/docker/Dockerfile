FROM python:3.12-slim

RUN pip install --no-cache-dir psutil PyYAML

RUN useradd -m -u 1000 runner
USER runner

WORKDIR /app
COPY runner_inside.py /app/runner_inside.py

ENTRYPOINT ["python", "/app/runner_inside.py"]

