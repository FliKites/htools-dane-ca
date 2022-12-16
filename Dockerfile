FROM python:3.8-slim

RUN pip install serles-acme

COPY . /app

WORKDIR /app

EXPOSE 8443

CMD ["python", "-m", "serles"]
