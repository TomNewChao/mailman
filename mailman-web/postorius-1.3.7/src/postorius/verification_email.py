# -*- coding: utf-8 -*-
# @Time    : 2023/1/17 11:29
# @Author  : Tom_zc
# @FileName: verification_email.py
# @Software: PyCharm
import time
import random
import hashlib
import json
import logging
from django.core.mail import send_mail
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

Template = '''
<div style="margin:10px 20px;font-size:12px;">
    <div>邮箱退订确认</div>
    <div>Email Address Unsubscribe Confirmation</div>
    <br>
    <div>您好，这是{domain}的邮件列表服务，我们收到来自您的邮箱的退订请求。</div>
    <div>Hello, this is the Mailman server at {domain}. We have received a unsubscribe request for your email address.</div>
    <br>
    <div>你可以通过点击以下链接来进行确认：</div>
    <div>You can click on the link below to Undergo verification:</div>
    <br>
    <div><a href="{link}">{link}</div>
    <br>
    <div>若您不想退订该邮箱，请忽略此消息。</div>
    <div>If you do not wish to register this email address, simply disregard this message.</div>
'''


class UnsubscribeEmailImp(object):
    unsubscribe_url = "https://{}/postorius/lists/{}/anonymous_unsubscribe?token={}"
    cache_key = "unsubscribe_{}"

    @classmethod
    def _next_unpredictable_id(cls):
        """Calculate a unique token."""
        right_now = time.time()
        x = random.random() + right_now % 1.0 + time.process_time() % 1.0
        return hashlib.sha1(repr(x).encode('utf-8')).hexdigest()

    @classmethod
    def get_uuid(cls):
        """get unpredicatable id"""
        for attempts in range(5):
            token = cls._next_unpredictable_id()
            # In practice, we'll never get a duplicate, but we'll be anal about checking anyway.
            key = cls.cache_key.format(token)
            if not cache.has_key(key):
                return token
        else:
            raise RuntimeError('Could not find a valid pendings token')

    @classmethod
    def send_message(cls, list_name, templates, recipient_list):
        """use mta to send msg"""
        if not isinstance(recipient_list, list):
            recipient_list = [recipient_list]
        subject = "Your confirmation is needed to leave the {} mailing list".format(list_name)
        from_email = settings.DEFAULT_FROM_EMAIL
        auth_user = settings.EMAIL_HOST_USER
        auth_password = settings.EMAIL_HOST_PASSWORD
        send_mail(subject, None, from_email, recipient_list, auth_user=auth_user, auth_password=auth_password,
                  html_message=templates)

    @classmethod
    def send_email(cls, email, list_id, token):
        """send the unsubscribe email to user"""
        web_domain = settings.SERVE_WEB_DOMAIN
        link = cls.unsubscribe_url.format(web_domain, list_id, token)
        list_name = list_id.replace(".", "@", 1)
        template = Template.format(
            domain=list_name,
            link=link
        )
        cls.send_message(list_name, template, email)

    @classmethod
    def set_token(cls, email, list_id, token):
        """storage token into redis"""
        save_dict = {
            "email": email,
            "list_id": list_id,
        }
        key = cls.cache_key.format(token)
        cache.set(key, json.dumps(save_dict), settings.VERIFICATION_CODE_EXPIRATION)
        return True

    @classmethod
    def get_token(cls, token):
        """parse text to obj"""
        dict_data = dict()
        key = cls.cache_key.format(token)
        value_str = cache.get(key)
        if value_str:
            dict_data = json.loads(value_str)
        return dict_data

    @classmethod
    def delete_key(cls, key, is_token=False):
        try:
            if is_token:
                key = cls.cache_key.format(key)
            cache.delete(key)
        except Exception as e:
            logger.error("e:{}".format(e))