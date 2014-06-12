#!/usr/bin/env python

from gevent import monkey; monkey.patch_all()

import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import logging as log
import os
import platform
import re
import smtplib
import time
from urlparse import parse_qs, urlparse

import gevent
import humanize
from pyquery import PyQuery as pq
import requests
from setproctitle import setproctitle
import yaml


log.basicConfig(level='INFO')

CFG = yaml.load(open('cfg.yml', 'r'))
TEMPLATE = u"""\
<html>
<head>
    <title>%s</title>
    <style>
        p, ul, ol, li, body { text-align: left; }
        .RIL_IMG { text-align: center; }
        img { display: inline-block; max-width: 100%%; vertical-align: center; }
    </style>
</head>
<body>
%s
</body>
</html>
"""

def read(filename):
    if not os.path.isfile(filename): return False
    with open(filename, 'r') as f:
        return f.read()

def write(filename, text):
    with open(filename, 'wb') as f:
        f.write(text)

def download(url, filename):
    try:
        write(filename, requests.get(url).content)
    except requests.HTTPError:
        log.error('Failed to download: %s', url)

def encode_url(url):
    return base64.b64encode(url).replace('/', '_')

def send(to, subject, body, attachments=()):
    author = 'kindler@textnot.es'
    msg = MIMEMultipart()
    msg['From'] = author
    msg['To'] = to
    msg['Subject'] = subject
    msg.attach(MIMEText(body.encode('utf-8'), _charset='utf-8'))

    for attach in attachments:
        part = MIMEApplication(attach)
        fn = 'article.mobi'
        part.add_header('Content-Disposition', 'attachment; filename="%s"' % fn)
        msg.attach(part)

    server = smtplib.SMTP()
    server.connect(CFG['smtp']['host'], CFG['smtp']['port'])
    server.login(CFG['smtp']['user'], CFG['smtp']['pass'])
    server.sendmail(author, (to,), msg.as_string())
    server.quit()

def auth():
    s = requests.Session()

    data = {
        'consumer_key': CFG['api']['consumer'],
        'redirect_uri': CFG['api']['redirect'],
    }
    r = s.post('https://getpocket.com/v3/oauth/request', data=data)
    code = parse_qs(r.text)['code'][0]

    data = {
        'mobile': 1,
        'request_token': code,
        'redirect_uri': CFG['api']['redirect'],
    }
    r = s.get('https://getpocket.com/auth/authorize', data=data)

    data = {
        'request_token': code,
        'approve_flag': 1,
        'permission': 'd',
        'redirect_uri': CFG['api']['redirect'],
    }
    r = s.post('https://getpocket.com/auth/approve_access', data=data)

    d = pq(r.text)
    data = {
        'feed_id': CFG['credentials']['user'],
        'password': CFG['credentials']['pass'],
        'form_check': d('input[name="form_check"]').val(),
        'source': d('input[name="source"]').val(),
        'route': d('input[name="route"]').val(),
    }
    r = s.post('https://getpocket.com/login_process', data=data)

    data = {
        'consumer_key': CFG['api']['consumer'],
        'code': code,
    }
    r = s.post('https://getpocket.com/v3/oauth/authorize', data=data)
    access_token = parse_qs(r.text)['access_token'][0]
    return s, access_token

def fetch(s, access_token):
    data = {
        'consumer_key': CFG['api']['consumer'],
        'access_token': access_token,
        'count': 10,
        'contentType': 'article',
        'detailType': 'complete',
        'sort': 'oldest',
    }
    since = read('.since')
    if since: data['since'] = since
    r = s.post('https://getpocket.com/v3/get', data=data)

    response = r.json()
    write('.since', str(response['since']))
    return response

def insert_imgs(d, imgs):
    imgurls = []
    for imgdiv in d('.RIL_IMG'):
        imgid = unicode(imgdiv.get('id')[8:])
        pq(imgdiv).append(pq('<img/>').attr('src', imgs[imgid]['src']))

    for imgel in d('img'):
        src = imgel.get('src')
        ext = os.path.splitext(urlparse(src).path)[1]
        if ext == '.php': ext = '.png'
        coded = '%s%s' % (encode_url(src), ext)
        cached = os.path.join('.cache', coded)
        imgurls.append((src, cached))
        pq(imgel).attr('src', coded)

    return imgurls

def replace_links(d, url):
    for a in d('a'):
        if not a.get('href').startswith(url + '#'):
            uel = pq('<u/>').text(a.text)
            suba = pq('<a>&uarr;</a>').attr('href', a.get('href'))
            uel.append(suba).insert_before(pq(a))
        pq(a).remove()

def main():
    s, access_token = auth()

    while True:
        response = fetch(s, access_token)
        if not os.path.isdir('.cache'):
            os.makedirs('.cache')

        for item_id in response['list']:
            url = 'http://getpocket.com/a/read/%s' % item_id
            readbody = s.get(url).text

            check = re.search(r"var formCheck = '(.+)'", readbody).group(1)
            data = {
                'itemId': item_id,
                'formCheck': check,
            }
            r = s.post('http://getpocket.com/a/x/getArticle.php', data=data)
            article = r.json()['article']
            d = pq(article['article'])
            imgurls = insert_imgs(d, article['images'])
            replace_links(d, article['resolvedUrl'])

            # Fetch all the image files in parallel via gevent.
            gs = [gevent.spawn(download, src, out) for (src, out) in imgurls]
            gevent.joinall(gs)

            htmlfile = '%s.html' % item_id
            html = TEMPLATE % (article['title'], d.html())
            htmlpath = os.path.join('.cache', htmlfile)
            write(htmlpath, html.encode('ascii', 'xmlcharrefreplace'))

            bindir = platform.system().lower()
            binpath = os.path.join('bin', bindir, 'kindlegen')
            os.system('%s "%s" > /dev/null' % (binpath, htmlpath))

            mobipath = os.path.join('.cache', '%s.mobi' % item_id)
            to = CFG['kindle']['email']
            body = 'Your Pocket article has been attached to this email.'
            mobidata = read(mobipath)
            send(to, article['title'], body, [mobidata])

            size = humanize.naturalsize(len(mobidata))
            log.info('Sent article "%s" (%s).', article['title'], size)

        time.sleep(30.0)

if __name__ == '__main__':
    setproctitle('kindler')
    main()
