FROM python:3.11-slim

WORKDIR /app

# curl is used by every CronJob's failure-alert hook (see k8s/*.yaml). Without it
# the alert dies with "curl: not found" and failures pass silently.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No CMD — each k8s CronJob specifies its own: python -m <stage>.main
