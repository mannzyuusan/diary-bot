#!/usr/bin/env bash
# Oracle Cloud などの Ubuntu サーバーで日記Botを常時起動させるセットアップ。
# 使い方: Botのフォルダで `bash deploy/setup.sh`（.env と diary.db などは先にコピーしておく）
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

# メモリ1GBのサーバーでも pip install が落ちないよう、スワップを用意する
if [ "$(free -m | awk '/^Mem:/{print $2}')" -lt 2000 ] && ! swapon --show | grep -q .; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sed "s#__DIR__#${DIR}#g; s#__USER__#$(whoami)#g" deploy/diary-bot.service \
  | sudo tee /etc/systemd/system/diary-bot.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now diary-bot
sudo systemctl status diary-bot --no-pager
