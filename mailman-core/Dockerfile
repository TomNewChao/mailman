FROM openeuler/openeuler:22.03
RUN python3 -m ensurepip --default-pip && python3 -m pip install --upgrade pip
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
RUN pip config set global.timeout 120

# 1.copy
RUN mkdir -p "/opt/mailman/package"
COPY . /opt/mailman/package/
RUN cp /opt/mailman/package/docker-entrypoint.sh /usr/local/bin/ && chmod +x /usr/local/bin/docker-entrypoint.sh

# 2.install
RUN yum update -y && yum install postgresql-odbc-devel -y && yum install mysql -y && pip3 install pymysql==1.0.2
RUN pip3 uninstall flufl.bounce flufl.i18n flufl.lock -y && pip3 install flufl.bounce flufl.i18n flufl.lock
RUN cd /opt/mailman/package/mailman-core-3.3.7 && python3 setup.py install
RUN cd /opt/mailman/package/mailman-hyperkitty-1.2.0 && python3 setup.py install
RUN mv /opt/mailman/package/email/base64mime.py /usr/lib64/python3.9/email/ -f && chmod +644 /usr/lib64/python3.9/email/base64mime.py
RUN mv /opt/mailman/package/email/quoprimime.py /usr/lib64/python3.9/email/ -f && chmod +644 /usr/lib64/python3.9/email/quoprimime.py
RUN rm -rf /opt/mailman/package
RUN pip3 install psycopg2-binary==2.9.5

# 3.setting
WORKDIR /opt/mailman
RUN useradd mailman -s /sbin/nologin -M
RUN chown -R mailman /opt/mailman
RUN ln -s /usr/local/bin/runner /usr/bin/runner  && ln -s /usr/local/bin/mailman /usr/bin/
EXPOSE 8001 8024
ENV MAILMAN_CONFIG_FILE /etc/mailman.cfg

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["master", "--force"]
