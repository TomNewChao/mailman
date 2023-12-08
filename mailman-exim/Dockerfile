FROM openeuler/openeuler:22.03-lts-sp2

ENV TINI_VERSION v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /usr/bin/tini
RUN chmod 755 /usr/bin/tini
RUN yum update -y && yum install -y exim
RUN ln -s /usr/sbin/exim /usr/sbin/exim4

COPY email-addresses /etc/email-addresses
COPY aliases /etc/aliases

WORKDIR /opt
USER exim:exim

