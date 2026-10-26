# Align the tag with the playwright version in requirements.txt (browsers must match the library).
ARG PW_TAG=v1.63.0-noble
FROM mcr.microsoft.com/playwright/python:${PW_TAG}

WORKDIR /suite
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Secrets are injected at runtime by the CI agent / orchestrator, never baked into the image.
ENV QA_ENV=sit
ENTRYPOINT ["pytest"]
CMD ["-m", "smoke"]
