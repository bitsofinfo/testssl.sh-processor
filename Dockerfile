FROM python:3.6.5-alpine

# install the processor and testssl.sh under /usr/local/bin
RUN apk update ; \
    apk upgrade ; \
    apk add git build-base bash bind-tools \
            coreutils \
            'curl>=7.58.0-r0' \
            'ncurses>=6.0_p20170930-r0' \
            tzdata ; \
    echo $PATH ; \
    git clone https://github.com/bitsofinfo/testssl.sh-processor.git ; \
    cp /testssl.sh-processor/*.py /usr/local/bin/ ; \
    rm -rf /testssl.sh-processor ; \
    pip install --upgrade pip twisted pyyaml python-dateutil watchdog ; \
    cd /tmp ; \
    git clone https://github.com/drwetter/testssl.sh.git ; \
    mv testssl.sh/* /usr/local/bin ; \
    rm -rf /tmp/testssl.sh ; \
    apk del git build-base ; \
    ls -al /usr/local/bin ; \
    rm -rf /var/cache/apk/* ; \
    chmod +x /usr/local/bin/*.py
