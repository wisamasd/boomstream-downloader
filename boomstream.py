#!/usr/bin/env python3

import argparse
import json
import os
import re
import string
import subprocess
import shutil
import sys

from base64 import b64decode
from lxml.html import fromstring
import requests

XOR_KEY = 'bla_bla_bla'

OUTPUT_PATH = "output"

VALID_FILENAME_CHARS = set(f" -_.(){string.ascii_letters}{string.digits}")

headers = {
  'authority': 'play.boomstream.com',
  'pragma': 'no-cache',
  'cache-control': 'no-cache',
  'upgrade-insecure-requests': '1',
  'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.92 Safari/537.36',
  'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
  'sec-fetch-site': 'none',
  'sec-fetch-mode': 'navigate',
  'sec-fetch-user': '?1',
  'sec-fetch-dest': 'document',
  'accept-language': 'en-US,en;q=0.9,ru;q=0.8,es;q=0.7,de;q=0.6'}

def valid_filename(s):
    return ''.join(c for c in s if c in VALID_FILENAME_CHARS or c.isalpha())

def output_path(path):
    return os.path.join(OUTPUT_PATH, path)

def ensure_folder_exists(path):
    if not os.path.exists(path):
        os.mkdir(path)

def run_bash(command):
    exit_code, output = subprocess.getstatusoutput(command)
    if exit_code != 0:
        print(output)
        raise ValueError(f'failed with exit code {exit_code}')
    return output

def run_bash_check_exist(command):
    exit_code, output = subprocess.getstatusoutput(command)
    return exit_code == 0

class App(object):

    def __init__(self):
        parser = argparse.ArgumentParser(description='boomstream.com downloader')
        parser.add_argument('--entity', type=str, required=True)
        parser.add_argument('--pin', type=str, required=False)
        parser.add_argument('--use-cache', action='store_true', required=False)
        parser.add_argument('--resolution', type=str, required=False)
        parser.add_argument('--openssl-path', type=str, required=False)
        parser.add_argument('--ffmpeg-path', type=str, required=False)
        parser.add_argument('--ffprobe-path', type=str, required=False)
        self.args = parser.parse_args()
        self.openssl = "openssl"
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"

    def get_token(self):
        return b64decode(self.config['mediaData']['token']).decode('utf-8')

    def get_m3u8_url(self):
        return b64decode(self.config['mediaData']['links']['hls']).decode('utf-8')

    def get_boomstream_config(self, page):
        """
        Evals value assigned to window.boomstreamConfig variable as JSON. This is ugly,
        but still better than using regular expressions to extract all needed variables
        from HTML page.
        """
        html = fromstring(page)
        result = None

        for script in html.xpath('//script[@type="text/javascript"]'):
            m = re.search("window.boomstreamConfig = ({.*});$", script.text_content(), flags=re.M)
            if m is not None:
                result = json.loads(m.group(1))

        if result is None:
            raise Exception("Could not get boomstreamConfig from the main page")

        with open(output_path('boomstream.config.json'), 'wt') as f:
            del result["translations"]
            f.write(json.dumps(result, ensure_ascii=False, indent=4))

        return result

    def get_playlist(self, url):
        if self.args.use_cache and os.path.exists(output_path('boomstream.playlist.m3u8')):
            with open(output_path('boomstream.playlist.m3u8')) as f:
                return f.read()

        r = requests.get(url, headers=headers)

        with open(output_path('boomstream.playlist.m3u8'), 'wt') as f:
            f.write(r.text)

        return r.text

    def res2int(self, resolution):
        if 'x' in resolution:
            return int(resolution.split('x')[0]) * int(resolution.split('x')[1])
        else:
            return int(resolution)

    def extract_chunklist_urls(self, playlist):
        result = []
        resolution = None

        for line in playlist.split('\n'):
            if line.startswith('#EXT-X-STREAM-INF'):
                m = re.search(r'RESOLUTION=(\d+x\d+)', line)
                if m is not None:
                    resolution = m.group(1)
                else:
                    m = re.search(r'BANDWIDTH=(\d+)', line)
                    if m is not None:
                        resolution = m.group(1)
                    else:
                        raise Exception("Could not get resolution from EXT-X-STREAM-INF")
            elif resolution is not None:
                result.append([resolution, line, self.res2int(resolution)])
                resolution = None

        return result

    def get_chunklist(self, playlist):
        all_chunklists = self.extract_chunklist_urls(playlist)
        print(f"This video is available in the following resolutions: {', '.join(i[0] for i in all_chunklists)}")

        if self.args.resolution is not None:
            url = None
            for item in all_chunklists:
                if item[0] == self.args.resolution:
                    url = item[1]
                    break
            if url is None:
                raise Exception("Playlist for resolution specifeid is --resolution " \
                                "argument is not found")
        else:
            # If the resolution is not specified in args, pick the best one
            url = sorted(all_chunklists, key=lambda x: x[2])[-1][1]

        print(f"URL: {url}")

        if url is None:
            raise Exception("Could not find chunklist in playlist data")

        if self.args.use_cache and os.path.exists(output_path('boomstream.chunklist.m3u8')):
            with open(output_path('boomstream.chunklist.m3u8')) as f:
                return f.read()

        r = requests.get(url, headers=headers)

        with open(output_path('boomstream.chunklist.m3u8'), 'wt') as f:
            f.write(r.text)

        return r.text

    def get_xmedia_ready(self, chunklist):
        """
        X-MEDIA-READY contains a value that is used to calculate IV for AES-128 and a URL
        to obtain AES-128 encryption key.
        """
        for line in chunklist.split('\n'):
            if line.split(':')[0] == '#EXT-X-MEDIA-READY':
                return line.split(':')[1]

        raise Exception("Could not find X-MEDIA-READY")

    def decrypt(self, source_text, key):
        result = ''
        while len(key) < len(source_text):
            key += key

        for n in range(0, len(source_text), 2):
            c = int(source_text[n:n+2], 16) ^ ord(key[(int(n / 2))])
            result = result + chr(c)

        return result

    def encrypt(self, source_text, key):
        result = ''

        while len(key) < len(source_text):
            key += key

        for i in range(0, len(source_text)):
            result += f'{ord(source_text[i]) ^ ord(key[i]):02x}'

        return result

    def get_aes_key(self, xmedia_ready):
        """
        Returns IV and 16-byte key which will be used to decrypt video chunks
        """
        decr = self.decrypt(xmedia_ready, XOR_KEY)
        print(f'Decrypted X-MEDIA-READY: {decr}')

        key = None
        iv = ''.join([f'{ord(c):02x}' for c in decr[20:36]])

        key_url = 'https://play.boomstream.com/api/process/' + \
                  self.encrypt(decr[0:20] + self.token, XOR_KEY)

        print(f'key url = {key_url}')
        print(f"IV = {iv}")

        return iv, key_url

    def download_chunks(self, chunklist, iv, key_url, title):
        valid_name = valid_filename(title)
        ensure_folder_exists(output_path(valid_name))

        filenames = []

        i = 0
        for line in chunklist.split('\n'):
            if not line.startswith('https://'):
                continue

            outf = output_path(os.path.join(valid_name, f"{i:05d}.ts"))
            outf_encrypted = output_path(os.path.join(valid_name, f"{i:05d}.tsencrypted"))
            filenames.append(outf)
            if os.path.exists(outf) and os.path.getsize(outf) > 0:
                i += 1
                print(f"Chunk #{i} exists [{outf}]")
                continue
            if os.path.exists(outf_encrypted) and os.path.getsize(outf_encrypted) > 0:
                os.remove(outf_encrypted)

            r = requests.get(key_url, headers=headers)
            key = r.text
            # Convert the key to format suitable for openssl command-line tool
            hex_key = ''.join([f'{ord(c):02x}' for c in key])

            print(f"Downloading chunk #{i}")
            file_crypt = requests.get(line)
            if file_crypt:
                with open(outf_encrypted, 'ab') as f:
                    f.write(file_crypt.content)
            run_bash(f'{self.openssl} aes-128-cbc -K "{hex_key}" -iv "{iv}" -d  -in "{outf_encrypted}" -out "{outf}"')
            if os.path.exists(outf_encrypted) and os.path.getsize(outf_encrypted) > 0:
                os.remove(outf_encrypted)
            i += 1
        return filenames

    def merge_chunks(self, filenames, expected_result_duration, title):
        """
        Merges all chunks into one file and encodes it to MP4
        """
        valid_name = valid_filename(title)
        merged_file_ts_filename = f"{output_path(valid_name)}.ts"
        print("Merging chunks...")
        with open(merged_file_ts_filename, 'wb') as merged:
            for ts_file in filenames:
                with open(ts_file, 'rb') as mergefile:
                    shutil.copyfileobj(mergefile, merged)

        print("Encoding to MP4")
        temp_mp4_filename = f'{output_path(valid_name)}.mp4'
        if self.exist_ffmpeg:
            run_bash(f'{self.ffmpeg} -nostdin -y -i "{merged_file_ts_filename}" -c copy "{temp_mp4_filename}"')

        if self.exist_ffprobe:
            result_format = run_bash(f'{self.ffprobe}  -i "{temp_mp4_filename}" -show_format')
            result_duration = float([line[len("duration="):] for line in result_format.split('\n') if line.startswith("duration=")][0])
            print(f"Result duration: {result_duration:.2f}")
            print(f"Expected duration: {expected_result_duration:.2f}")
            if abs(result_duration - expected_result_duration) > 2:
                raise ValueError(f"unexpected result duration: {expected_result_duration:.2f} != {result_duration:.2f}")

        if self.exist_ffmpeg:
            ensure_folder_exists(output_path("results"))
            result_filename = output_path(os.path.join("results", f"{valid_name}.mp4"))
            if os.path.exists(result_filename):
                os.remove(result_filename)
            os.rename(temp_mp4_filename, result_filename)
            print(f"Result: {result_filename}")

    def get_title(self):
        return self.config['entity']['title']

    def get_access_cookies(self):
        pin = self.args.pin
        if pin is None:
            return {}
        r = requests.post("https://play.boomstream.com/api/subscriptions/recovery",
                          headers={'content-type': 'application/json;charset=UTF-8'},
                          data=f'{{"entity":"{self.args.entity}","code":"{pin}"}}')
        response = json.loads(r.text)
        if "data" not in response or "cookie" not in response["data"]:
            if "errors" not in response or "code" not in response:
                raise ValueError(f"unexpected response on authorization: {r.text}")
            else:
                raise ValueError(f"authorization failed: {response['code']} {response['errors']}")
        cookie = response["data"]["cookie"]
        return {cookie["name"]: cookie["value"]}

    def run(self):
        if self.args.openssl_path:
            self.openssl = f'"{self.args.openssl_path}"'
        if self.args.ffmpeg_path:
            self.ffmpeg = f'"{self.args.ffmpeg_path}"'
        if self.args.ffprobe_path:
            self.ffprobe = f'"{self.args.ffprobe_path}"'

        self.exist_openssl = run_bash_check_exist(f'{self.openssl} version')
        self.exist_ffmpeg = run_bash_check_exist(f'{self.ffmpeg} -version')
        self.exist_ffprobe = run_bash_check_exist(f'{self.ffprobe} -version')

        if not self.exist_openssl:
            raise ValueError('OpenSSL is not exist. Install OpenSSL or use arg "--openssl-path PATH"')

        if not self.exist_ffmpeg or not self.exist_ffprobe:
            print("http://ffmpeg.org/download.html")
            if not self.exist_ffmpeg:
                print(f'{self.ffmpeg} not exist. The result will not be converted to *.mp4.\nPlease Download ffmpeg and put it in the same directory as the script or use arg "--ffmpeg-path PATH".')
            if not self.exist_ffprobe:
                print(f'{self.ffprobe} not exist. The result duration will not be checked.\nPlease Download ffprobe and put it in the same directory as the script or use arg "--ffprobe-path PATH".')

        ensure_folder_exists(OUTPUT_PATH)

        cookies = self.get_access_cookies()

        result_path = output_path('result.html')

        if self.args.use_cache and os.path.exists(result_path):
            page = open(result_path).read()
        else:
            r = requests.get(f'https://play.boomstream.com/{self.args.entity}', headers=headers, cookies=cookies)

            with open(result_path, 'wt') as f:
                f.write(r.text)

            page = r.text

        self.config = self.get_boomstream_config(page)
        if "mediaData" not in self.config or "duration" not in self.config['mediaData']:
            raise ValueError(
                "Video config is not available. Probably, the live streaming has not finished yet, or you use "
                "an incorrect pin code. If you're sure that translation is finished and pin code is correct, please "
                "create an issue in project github tracker and attach your boomstream.config.json file.")
        self.token = self.get_token()
        self.m3u8_url = self.get_m3u8_url()
        self.expected_result_duration = float(self.config['mediaData']['duration'])

        print(f"Token = {self.token}")
        print(f"Playlist: {self.m3u8_url}")

        playlist = self.get_playlist(self.m3u8_url)
        chunklist = self.get_chunklist(playlist)

        xmedia_ready = self.get_xmedia_ready(chunklist)

        print(f'X-MEDIA-READY: {xmedia_ready}')

        iv, key_url = self.get_aes_key(xmedia_ready)
        title = self.get_title()
        print(f"Title = {title}")

        filenames = self.download_chunks(chunklist, iv, key_url, title)
        self.merge_chunks(filenames, self.expected_result_duration, title)

if __name__ == '__main__':
    app = App()
    sys.exit(app.run())
