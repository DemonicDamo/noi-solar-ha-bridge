FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY noi_solar_ha_bridge.py ./
USER nobody
CMD ["python", "/app/noi_solar_ha_bridge.py"]
