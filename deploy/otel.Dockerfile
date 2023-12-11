FROM otel/opentelemetry-collector-contrib:0.53.0

COPY otel-collector-config.yml /config.yml
EXPOSE 4317

CMD ["--config", "/config.yml"]
