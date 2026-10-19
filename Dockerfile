FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY openbi ./openbi
RUN pip install --no-cache-dir . && useradd --create-home openbi
USER openbi
ENV OPENBI_HOME=/data
VOLUME ["/data"]
EXPOSE 8000
# For sample users and content instead: docker run -p 8000:8000 IMAGE openbi demo --host 0.0.0.0 --port 8000
CMD ["openbi", "serve", "--host", "0.0.0.0", "--port", "8000"]
