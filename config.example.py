"""
Örnek yapılandırma. Bu dosyayı `config.py` olarak kopyalayıp gerçek değerleri girin:
    cp config.example.py config.py
config.py ASLA commit edilmez (.gitignore'da).
"""

# ============================================================
# BİZİM BINANCE FUTURES HESABIMIZ (emir yürütme)
# ============================================================
BINANCE_API_KEY = "YOUR_API_KEY"
BINANCE_API_SECRET = "YOUR_API_SECRET"
USE_TESTNET = True   # Önce True ile testnet'te dene. Canlı için False.

# ============================================================
# TRADER SCRAPER (CDP — gerçek Chrome'a bağlanır)
# ============================================================
# İzlenecek smart-money trader profili:
# https://www.binance.com/en/smart-money/profile/<PROFILE_ID>
TOP_TRADER_ID = "PASTE_BINANCE_SMART_MONEY_PROFILE_ID"

# Bot, --remote-debugging-port=9222 ile açılmış Chrome'a bağlanır (bkz. README / baslat.sh).
CDP_URL = "http://127.0.0.1:9222"

# ============================================================
# AYAR / RİSK PARAMETRELERİ
# ============================================================
POLL_INTERVAL_SEC = 5            # Döngü aralığı (saniye)
DRY_RUN = True                   # True iken emir GÖNDERMEZ, sadece loglar (güvenli)
REBALANCE_TOLERANCE = 0.10       # Hedef boyut %10'dan az saparsa emir gönderme (whipsaw önler)
SIZE_MULTIPLIER = 1.0            # hedef = trader_size × ölçek × bu çarpan (risk artırır)
MANUAL_BALANCE = 0.0            # >0 ise ölçek bu bakiyeyle hesaplanır; 0 = canlı bakiye
FOLLOW_STATE_FILE = ".follow_state.json"
API_TOKEN = "BURAYA_RASTGELE_GIZLI_BIR_DEGER"  # localhost API + eklenti gizli anahtarı
TELEGRAM_TOKEN = ""      # BotFather token (boş=bildirim kapalı)
TELEGRAM_CHAT_ID = ""    # senin chat id'in
ALERT_LOSS_USDT = 0.0    # >0: yönetilen toplam zarar bu USDT'yi geçince uyar
MIN_NOTIONAL_USDT = 5.0          # Binance min emir değeri; altındaki emirler atlanır
COPY_COIN_M = False              # CM (coin-margined) pozisyonları da kopyala
SKIP_SYMBOLS = ["XAUUSDT"]       # kopyalanmayacak semboller (TradFi-Perps vb.)
FOLLOW_STATE_FILE = ".follow_state.json"  # takip seçimleri kalıcı dosyası
