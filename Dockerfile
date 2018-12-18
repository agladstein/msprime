#maintainer Ariella Gladstein
#organization tskit-dev
#application "Msprime: A reimplementation of Hudson's classical ms simulator for modern data sets."

FROM ubuntu:18.04

# Set the working directory to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

RUN apt-get update && apt-get install -y --no-install-recommends \
            python3 \
            python-dev \
            python3-dev \
            python-pip \
            python3-pip \
            build-essential \
            libgsl0-dev \
            libssl-dev \
            libffi-dev \
            libxml2-dev \
            libxslt1-dev \
            zlib1g-dev \
            git \
            && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install --upgrade setuptools
RUN pip install --upgrade setuptools
RUN pip3 install .
RUN pip install .

# build info
RUN echo "Timestamp:" `date --utc` | tee /image-build-info.txt



