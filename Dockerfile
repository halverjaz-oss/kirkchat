FROM python:3.12-slim
WORKDIR /app
COPY . .
EXPOSE ${PORT:-8000}
CMD ["python", "server.py"]
