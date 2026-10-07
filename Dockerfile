FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway_ondemand.py capture.py ./
COPY examples/ ./examples/
EXPOSE 8121
CMD ["python", "gateway_ondemand.py"]
