FROM openeuler/openeuler:22.03
RUN python3 -m ensurepip --default-pip && python3 -m pip install --upgrade pip
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
RUN pip config set global.timeout 120

# 1.copy
COPY . /opt
RUN chmod +x /opt/cancel_subscribe_pending.py

# 2. install
RUN pip3 install mailmanclient

CMD ["python3", "/opt/cancel_subscribe_pending.py"]