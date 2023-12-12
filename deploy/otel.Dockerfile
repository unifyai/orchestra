FROM otel/opentelemetry-collector-contrib:0.91.0

COPY otel-collector-config.yml /config.yml
ENV JAEGER_ENDPOINT="jaeger:4317"
EXPOSE 4317

CMD ["--config", "/config.yml"]
