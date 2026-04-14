#!/usr/bin/env python3
"""BTC 5分钟交易策略 - 合并版 (GUI)"""
import asyncio, json, time, requests, websockets, ssl, re, os, sys, threading, copy
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone, timedelta
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from web3 import Web3
from web3.constants import MAX_INT

def get_script_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "trading": {
        "threshold": 0.55, "stop_loss": 0.50, "hedge_profit": 0.05,
        "no_entry": 0.85, "size": 15, "no_trade_start": 21, "no_trade_end": 22,
        "buy_interval": 15
    }
}

def load_config():
    path = os.path.join(get_script_dir(), 'config.json')
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)

def save_config(config):
    path = os.path.join(get_script_dir(), 'config.json')
    with open(path, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

# Polymarket 合约地址
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CONTRACTS = [
    ("CTF Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
]

ERC20_ABI = [{"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
              "name": "approve", "outputs": [{"name": "", "type": "bool"}], "payable": False, "stateMutability": "nonpayable", "type": "function"}]
ERC1155_ABI = [{"inputs": [{"internalType": "address", "name": "operator", "type": "address"}, {"internalType": "bool", "name": "approved", "type": "bool"}],
                "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]

def setup_allowances(private_key, log_callback=None):
    """设置链上授权（仅需执行一次）"""
    log = log_callback or print

    web3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not web3.is_connected():
        log("❌ 无法连接到 Polygon RPC")
        return False

    account = web3.eth.account.from_key(private_key)
    pub_key = account.address
    log(f"钱包地址: {pub_key}")

    matic_balance = web3.eth.get_balance(pub_key) / 1e18
    log(f"MATIC 余额: {matic_balance:.4f}")
    if matic_balance < 0.01:
        log("⚠️ MATIC 余额可能不足以支付 gas")

    usdc = web3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
    ctf = web3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)

    log("\n开始设置授权（共 6 笔交易）...")
    for idx, (name, contract_addr) in enumerate(CONTRACTS, 1):
        log(f"\n[{idx}/3] {name}")

        try:
            nonce = web3.eth.get_transaction_count(pub_key)
            log(f"  授权 USDC...")
            usdc_tx = usdc.functions.approve(contract_addr, int(MAX_INT, 0)).build_transaction({
                "chainId": CHAIN_ID, "from": pub_key, "nonce": nonce, "gas": 100000, "gasPrice": web3.eth.gas_price
            })
            signed_usdc = web3.eth.account.sign_transaction(usdc_tx, private_key=private_key)
            usdc_hash = web3.eth.send_raw_transaction(signed_usdc.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(usdc_hash, timeout=600)
            if receipt['status'] == 1:
                log(f"  ✅ USDC 授权成功")
            else:
                log(f"  ❌ USDC 授权失败")
                return False
        except Exception as e:
            log(f"  ❌ USDC 授权出错: {e}")
            return False

        try:
            nonce = web3.eth.get_transaction_count(pub_key)
            log(f"  授权 CTF...")
            ctf_tx = ctf.functions.setApprovalForAll(contract_addr, True).build_transaction({
                "chainId": CHAIN_ID, "from": pub_key, "nonce": nonce, "gas": 100000, "gasPrice": web3.eth.gas_price
            })
            signed_ctf = web3.eth.account.sign_transaction(ctf_tx, private_key=private_key)
            ctf_hash = web3.eth.send_raw_transaction(signed_ctf.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(ctf_hash, timeout=600)
            if receipt['status'] == 1:
                log(f"  ✅ CTF 授权成功")
            else:
                log(f"  ❌ CTF 授权失败")
                return False
        except Exception as e:
            log(f"  ❌ CTF 授权出错: {e}")
            return False

    log("\n🎉 所有授权设置完成！")
    return True

def create_clob_client(private_key):
    """用私钥创建并认证 ClobClient"""
    client = ClobClient(HOST, CHAIN_ID, key=private_key, signature_type=0)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


class PriceWatcher:
    def __init__(self, on_price_update=None):
        self.ws = None
        self.yes_tok = None
        self.no_tok = None
        self.last_prices = (0.5, 0.5)
        self.running = False
        self.lock = asyncio.Lock()
        self.on_price_update = on_price_update
        self._listen_task = None

    async def start(self, yes_tok, no_tok):
        self.yes_tok = yes_tok
        self.no_tok = no_tok
        self.running = True
        self._listen_task = asyncio.create_task(self._listen())

    async def _listen(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        while self.running:
            try:
                if not self.ws:
                    self.ws = await websockets.connect(
                        'wss://ws-subscriptions-clob.polymarket.com/ws/market',
                        ssl=ssl_ctx
                    )
                    await self.ws.send(json.dumps({
                        "assets_ids": [self.yes_tok, self.no_tok],
                        "type": "market"
                    }))
                async for msg in self.ws:
                    if not self.running:
                        break
                    try:
                        data = json.loads(msg)
                        if "price" in data:
                            price = float(data["price"])
                            tok = data.get("asset_id", "")
                            async with self.lock:
                                if tok == self.yes_tok:
                                    self.last_prices = (price, self.last_prices[1])
                                elif tok == self.no_tok:
                                    self.last_prices = (self.last_prices[0], price)
                            if self.on_price_update:
                                self.on_price_update(self.last_prices)
                    except (json.JSONDecodeError, ValueError):
                        pass
            except Exception:
                self.ws = None
                if self.running:
                    await asyncio.sleep(1)

    async def get_price(self):
        async with self.lock:
            return self.last_prices

    async def stop(self):
        self.running = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


def get_token(window):
    try:
        url = f"https://polymarket.com/event/btc-updown-5m-{window}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return None, None
        ids = list(set(re.findall(r'\b(\d{50,})\b', r.text)))
        if len(ids) >= 2:
            return ids[0], ids[1]
    except Exception:
        pass
    return None, None

async def get_token_async(window):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_token, window)


class TradingEngine:
    def __init__(self, client, log_callback=None):
        self.client = client  # ClobClient instance
        self.watcher = PriceWatcher()
        self.log = log_callback or print
        # 状态
        self.up_shares = 0
        self.down_shares = 0
        self.up_hedges = []
        self.down_hedges = []
        self.last_window = 0
        self.yes_tok = None
        self.no_tok = None
        self.running = False
        self.loop = None
        self.last_buy_time = 0  # 上次买入时间
        # 参数（从config加载，GUI可修改）
        self.threshold = 0.55
        self.stop_loss = 0.50
        self.hedge_profit = 0.05
        self.no_entry = 0.85
        self.size = 15
        self.no_trade_start = 21
        self.no_trade_end = 22
        self.buy_interval = 15  # 买入间隔（秒）
        # 实时状态（GUI读取）
        self.up_price = 0.5
        self.down_price = 0.5
        self.remaining = 0

    def update_params(self, config):
        t = config.get('trading', {})
        self.threshold = t.get('threshold', 0.55)
        self.stop_loss = t.get('stop_loss', 0.50)
        self.hedge_profit = t.get('hedge_profit', 0.05)
        self.no_entry = t.get('no_entry', 0.85)
        self.size = t.get('size', 15)
        self.no_trade_start = t.get('no_trade_start', 21)
        self.no_trade_end = t.get('no_trade_end', 22)
        self.buy_interval = t.get('buy_interval', 10)

    def is_trading_allowed(self):
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz)
        hour, minute = now.hour, now.minute
        if hour == self.no_trade_start and minute >= 30:
            return False
        if hour == self.no_trade_end and minute < 30:
            return False
        # 修正：用 < 而非 <= 避免阻止整个起始小时
        if self.no_trade_start < hour < self.no_trade_end:
            return False
        return True

    def _send_order_sync(self, token, side, price, size):
        try:
            clob_side = BUY if side == "buy" else SELL
            order_args = OrderArgs(
                token_id=token,
                price=round(price, 2),
                size=size,
                side=clob_side,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)
            if resp and resp.get("success"):
                self.log(f"  ✅ {side} {size}张 @ {price:.2f} 成功")
                return {"success": True, "data": resp}
            else:
                error = resp.get("errorMsg", str(resp)) if resp else "无响应"
                self.log(f"  ❌ {side} 失败: {error}")
                return {"success": False, "error": error}
        except Exception as e:
            self.log(f"  ❌ {side} 异常: {e}")
            return {"success": False, "error": str(e)}

    async def _send_order(self, token, side, price, size):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_order_sync, token, side, price, size)

    async def _run(self):
        self.log("交易引擎启动")
        while self.running:
            current_ts = int(time.time())
            window = (current_ts // 300) * 300

            # 窗口切换
            if window != self.last_window:
                self.log(f"\n=== 新窗口 {datetime.now().strftime('%H:%M')} ===")
                self.last_window = window
                self.up_hedges = []
                self.down_hedges = []
                self.up_shares = 0
                self.down_shares = 0
                await self.watcher.stop()
                self.yes_tok, self.no_tok = await get_token_async(window)
                if self.yes_tok:
                    await self.watcher.start(self.yes_tok, self.no_tok)
                    self.log(f"Token已获取，WebSocket已连接")
                else:
                    self.log(f"⚠ 无法获取Token，等待重试...")
                await asyncio.sleep(2)
                continue

            if not self.yes_tok:
                self.yes_tok, self.no_tok = await get_token_async(window)
                if not self.yes_tok:
                    await asyncio.sleep(5)
                    continue

            up_price, down_price = await self.watcher.get_price()
            self.up_price = up_price
            self.down_price = down_price
            self.remaining = window + 300 - current_ts

            t = datetime.now().strftime("%H:%M:%S")
            self.log(f"[{t}] UP={up_price*100:.0f}% DOWN={down_price*100:.0f}% "
                     f"剩{self.remaining}s | 持仓:UP={self.up_shares} DOWN={self.down_shares}")

            # 99%止盈
            if up_price >= 0.99 and self.up_shares > 0:
                self.log(f"  🎯 UP达到99% → 卖出UP {self.up_shares}张")
                result = await self._send_order(self.yes_tok, "sell", 0.01, self.up_shares)
                if result.get("success"):
                    self.up_shares = 0
                    self.up_hedges = []
            elif down_price >= 0.99 and self.down_shares > 0:
                self.log(f"  🎯 DOWN达到99% → 卖出DOWN {self.down_shares}张")
                result = await self._send_order(self.no_tok, "sell", 0.01, self.down_shares)
                if result.get("success"):
                    self.down_shares = 0
                    self.down_hedges = []

            # 止损
            if up_price < self.stop_loss and self.up_hedges:
                total = sum(h["shares"] for h in self.up_hedges)
                self.log(f"  🛑 UP止损! {up_price*100:.0f}%<{self.stop_loss*100:.0f}% → 买DOWN x{total}")
                result = await self._send_order(self.no_tok, "buy", down_price, total)
                if result.get("success"):
                    self.down_shares += total
                    self.up_hedges = []

            if down_price < self.stop_loss and self.down_hedges:
                total = sum(h["shares"] for h in self.down_hedges)
                self.log(f"  🛑 DOWN止损! {down_price*100:.0f}%<{self.stop_loss*100:.0f}% → 买UP x{total}")
                result = await self._send_order(self.yes_tok, "buy", up_price, total)
                if result.get("success"):
                    self.up_shares += total
                    self.down_hedges = []

            # 对冲
            for hedge in self.up_hedges[:]:
                if up_price >= hedge["price"] + self.hedge_profit:
                    hedge_price = 1.0 - (hedge["price"] + self.hedge_profit)
                    self.log(f"  🎯 UP对冲 → 买DOWN x{hedge['shares']} @ {hedge_price:.2f}")
                    result = await self._send_order(self.no_tok, "buy", hedge_price, hedge["shares"])
                    if result.get("success"):
                        self.down_shares += hedge["shares"]
                        self.up_hedges.remove(hedge)

            for hedge in self.down_hedges[:]:
                if down_price >= hedge["price"] + self.hedge_profit:
                    hedge_price = 1.0 - (hedge["price"] + self.hedge_profit)
                    self.log(f"  🎯 DOWN对冲 → 买UP x{hedge['shares']} @ {hedge_price:.2f}")
                    result = await self._send_order(self.yes_tok, "buy", hedge_price, hedge["shares"])
                    if result.get("success"):
                        self.up_shares += hedge["shares"]
                        self.down_hedges.remove(hedge)

            # 建仓
            if self.is_trading_allowed():
                current_time = time.time()
                if len(self.up_hedges) >= 2 or len(self.down_hedges) >= 2:
                    pass  # 对冲队列已满，暂停建仓
                elif up_price >= self.threshold and up_price < self.no_entry:
                    # 检查买入间隔
                    if current_time - self.last_buy_time >= self.buy_interval:
                        self.log(f"  🟢 UP>{self.threshold*100:.0f}% → 买UP @ {up_price:.2f} x{self.size}")
                        result = await self._send_order(self.yes_tok, "buy", up_price, self.size)
                        if result.get("success"):
                            self.up_shares += self.size
                            self.up_hedges.append({"price": up_price, "shares": self.size})
                            self.last_buy_time = current_time
                elif down_price >= self.threshold and down_price < self.no_entry:
                    # 检查买入间隔
                    if current_time - self.last_buy_time >= self.buy_interval:
                        self.log(f"  🟢 DOWN>{self.threshold*100:.0f}% → 买DOWN @ {down_price:.2f} x{self.size}")
                        result = await self._send_order(self.no_tok, "buy", down_price, self.size)
                        if result.get("success"):
                            self.down_shares += self.size
                            self.down_hedges.append({"price": down_price, "shares": self.size})
                            self.last_buy_time = current_time
            else:
                self.log(f"  ⏸ {self.no_trade_start}:30-{self.no_trade_end}:30 禁止交易时段")

            await asyncio.sleep(5)

        await self.watcher.stop()
        self.log("交易引擎已停止")

    def start(self):
        self.running = True
        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._run())
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False


class TradingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BTC 5分钟交易策略")
        self.geometry("780x600")
        self.resizable(True, True)
        self.config_data = load_config()
        self.engine = None
        self.trader = None
        self.entries = {}
        self._build_ui()
        self._load_config_to_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=5, pady=5)

        # === 左侧：参数配置 ===
        left = ttk.LabelFrame(top, text="参数配置")
        left.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 5))

        api_fields = [("私钥", "private_key")]
        for i, (label, key) in enumerate(api_fields):
            ttk.Label(left, text=label + ":").grid(row=i, column=0, sticky=tk.W, padx=2, pady=1)
            e = ttk.Entry(left, width=28, show="*")
            e.grid(row=i, column=1, padx=2, pady=1)
            self.entries[key] = e

        trading_fields = [
            ("买入阈值", "threshold"), ("止损线", "stop_loss"),
            ("对冲利润", "hedge_profit"), ("禁入阈值", "no_entry"),
            ("每次数量", "size"), ("禁交易开始(时)", "no_trade_start"),
            ("禁交易结束(时)", "no_trade_end"), ("买入间隔(秒)", "buy_interval"),
        ]
        offset = len(api_fields)
        for i, (label, key) in enumerate(trading_fields):
            ttk.Label(left, text=label + ":").grid(row=offset + i, column=0, sticky=tk.W, padx=2, pady=1)
            e = ttk.Entry(left, width=10)
            e.grid(row=offset + i, column=1, sticky=tk.W, padx=2, pady=1)
            self.entries[key] = e

        btn_frame = ttk.Frame(left)
        btn_frame.grid(row=offset + len(trading_fields), column=0, columnspan=2, pady=5)
        self.start_btn = ttk.Button(btn_frame, text="▶ 启动", command=self._start_trading)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="■ 停止", command=self._stop_trading, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.auth_btn = ttk.Button(btn_frame, text="🔐 授权", command=self._setup_allowances)
        self.auth_btn.pack(side=tk.LEFT, padx=5)

        # === 右侧：实时状态 ===
        right = ttk.LabelFrame(top, text="实时状态")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.status_labels = {}
        status_items = [
            ("UP价格", "up_price"), ("DOWN价格", "down_price"),
            ("剩余时间", "remaining"), ("UP持仓", "up_shares"),
            ("DOWN持仓", "down_shares"), ("UP对冲队列", "up_hedges"),
            ("DOWN对冲队列", "down_hedges"),
        ]
        for i, (label, key) in enumerate(status_items):
            ttk.Label(right, text=label + ":").grid(row=i, column=0, sticky=tk.W, padx=5, pady=2)
            lbl = ttk.Label(right, text="--")
            lbl.grid(row=i, column=1, sticky=tk.W, padx=5, pady=2)
            self.status_labels[key] = lbl

        # === 底部：日志 ===
        log_frame = ttk.LabelFrame(self, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _load_config_to_ui(self):
        t = self.config_data.get('trading', {})
        for key in ['threshold', 'stop_loss', 'hedge_profit', 'no_entry', 'size', 'no_trade_start', 'no_trade_end', 'buy_interval']:
            self.entries[key].insert(0, str(t.get(key, '')))

    def _save_config_from_ui(self):
        t = self.config_data.setdefault('trading', {})
        for key in ['threshold', 'stop_loss', 'hedge_profit', 'no_entry']:
            try: t[key] = float(self.entries[key].get())
            except ValueError: pass
        for key in ['size', 'no_trade_start', 'no_trade_end', 'buy_interval']:
            try: t[key] = int(self.entries[key].get())
            except ValueError: pass
        save_config(self.config_data)

    def _log(self, msg):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.after(0, _append)

    def _setup_allowances(self):
        """授权按钮回调"""
        pk = self.entries['private_key'].get().strip()
        if not pk.startswith('0x'):
            pk = '0x' + pk
        if len(pk) != 66:
            messagebox.showerror("错误", "私钥格式错误（需要64位hex，可带0x前缀）")
            return

        if not messagebox.askyesno("确认", "授权操作会发送 6 笔链上交易，需要支付 gas 费用。\n确定继续吗？"):
            return

        self.auth_btn.config(state=tk.DISABLED)
        self._log("开始设置授权...")

        def run_auth():
            try:
                success = setup_allowances(pk, log_callback=self._log)
                if success:
                    self.after(0, lambda: messagebox.showinfo("成功", "授权设置完成！"))
                else:
                    self.after(0, lambda: messagebox.showerror("失败", "授权设置失败，请查看日志"))
            except Exception as e:
                self._log(f"❌ 授权异常: {e}")
                self.after(0, lambda: messagebox.showerror("错误", f"授权异常: {e}"))
            finally:
                self.after(0, lambda: self.auth_btn.config(state=tk.NORMAL))

        threading.Thread(target=run_auth, daemon=True).start()

    def _start_trading(self):
        if self.engine and self.engine.running:
            return
        self._save_config_from_ui()
        pk = self.entries['private_key'].get().strip()
        if not pk.startswith('0x'):
            pk = '0x' + pk
        if len(pk) != 66:
            messagebox.showerror("错误", "私钥格式错误（需要64位hex，可带0x前缀）")
            return

        try:
            self._log("正在连接 Polymarket API 并派生密钥...")
            client = create_clob_client(pk)
            self._log(f"✅ API 认证成功")
        except Exception as e:
            messagebox.showerror("错误", f"连接失败: {e}")
            return

        self.engine = TradingEngine(client, log_callback=self._log)
        self.engine.update_params(self.config_data)
        self.engine.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        for e in self.entries.values():
            e.config(state=tk.DISABLED)
        self._update_status()

    def _stop_trading(self):
        if self.engine:
            self.engine.stop()
            self._log("交易已停止")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        for e in self.entries.values():
            e.config(state=tk.NORMAL)

    def _update_status(self):
        if self.engine and self.engine.running:
            self.status_labels['up_price'].config(text=f"{self.engine.up_price*100:.1f}%")
            self.status_labels['down_price'].config(text=f"{self.engine.down_price*100:.1f}%")
            self.status_labels['remaining'].config(text=f"{self.engine.remaining}s")
            self.status_labels['up_shares'].config(text=str(self.engine.up_shares))
            self.status_labels['down_shares'].config(text=str(self.engine.down_shares))
            self.status_labels['up_hedges'].config(text=f"{len(self.engine.up_hedges)}/2")
            self.status_labels['down_hedges'].config(text=f"{len(self.engine.down_hedges)}/2")
            self.after(1000, self._update_status)

    def _on_close(self):
        if self.engine and self.engine.running:
            self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    app = TradingApp()
    app.mainloop()
