FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
RUN pip install uv

# Copy project files
COPY pyproject.toml README.md ./
COPY repowire/ repowire/

# Install repowire with relay dependencies
RUN uv pip install --system ".[relay]"

# Run relay server
EXPOSE 8000
CMD ["repowire", "relay", "start", "--host", "0.0.0.0", "--port", "8000"]
