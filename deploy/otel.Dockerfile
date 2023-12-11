FROM otel/opentelemetry-collector-contrib:0.53.0

COPY otel-collector-config.yml /config.yml
ENV JAEGER_ENDPOINT="jaeger:14250"
EXPOSE 4317

CMD ["--config", "/config.yml"]
