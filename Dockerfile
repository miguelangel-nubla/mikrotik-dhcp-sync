FROM python:3

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV CONFIG_DIR=/app/config
ENV LOG_LEVEL=INFO

CMD [ "python", "./run.py" ]
