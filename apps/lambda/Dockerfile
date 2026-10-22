ARG LAMBDA_BASE_IMAGE=public.ecr.aws/lambda/python:3.11@sha256:af7d68f0baca0969f5232b1adce5288b5132c711e4c9f6f899c8108d17012989

FROM ${LAMBDA_BASE_IMAGE}

# Copy the entire project for package installation
COPY . ${LAMBDA_TASK_ROOT}

# Install dependencies and the local cleanarr package
RUN pip install --no-cache-dir .

# Set the handler
CMD ["apps.lambda.main.lambda_handler"]
