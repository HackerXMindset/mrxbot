# Use the official Python 3.11 slim image as the base
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port the app runs on (default to 8091, but can be overridden)
EXPOSE 8091

# Set environment variables (optional, can be overridden in Koyeb)
ENV PORT=8091

# Run the application using gunicorn with eventlet, binding to $PORT
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:$PORT", "main:app"]
