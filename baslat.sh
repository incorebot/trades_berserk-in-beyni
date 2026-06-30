#!/bin/bash
# Botu ve arayüzü başlat.  Kullanım:  ./baslat.sh
# Ön koşul (bir kez): Chrome'a extension/ klasörünü "Load unpacked" ile yükle
# ve Binance'e girili bir sekme açık olsun.
cd "$(dirname "$0")" || exit 1
echo "🤖 Bot + arayüz başlatılıyor -> http://127.0.0.1:8777"
echo "   (Chrome'da eklenti yüklü ve Binance sekmesi açık/girili olmalı)"
echo "----------------------------------------------------------"
python3 app.py
