FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pokedeals.py config.yaml ./
ENV POKEDEALS_DB=/data/pokedeals.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "pokedeals.py"]
