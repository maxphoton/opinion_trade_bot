# Opinion.trade Market Making Bot

A Telegram bot for placing limit orders on [Opinion.trade](https://app.opinion.trade) prediction markets. The bot provides an intuitive interface for market making strategies with secure credential management.

## Features

### 🔐 Secure Registration
- **Encrypted Storage**: All sensitive data (wallet address, private key, API key) is encrypted using AES-GCM encryption
- **SQLite Database**: User credentials are stored locally in an encrypted SQLite database
- **Zero Trust**: Your private keys never leave your server in unencrypted form

### 📊 Market Order Placement
- **Interactive Flow**: Step-by-step process for placing limit orders
- **Market Analysis**: View market information including:
  - Best bid/ask prices for YES and NO tokens
  - Spread and liquidity metrics
  - Top 5 bids and asks with price visualization
- **Smart Validation**: Automatic balance checks and price validation
- **Categorical Markets**: Support for multi-outcome markets with submarket selection

### 💰 Order Management
- **Price Offset in Cents**: Set order prices relative to best bid using intuitive cent-based offsets
- **Direction Selection**: Choose BUY (below current price) or SELL (above current price)
- **Order Confirmation**: Review all settings before placing orders

## Getting Started

### Prerequisites

- Python 3.13+
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Opinion Labs API Key (obtain from [the form](https://docs.google.com/forms/d/1h7gp8UffZeXzYQ-lv4jcou9PoRNOqMAQhyW4IwZDnII/viewform?edit_requested=true))
- BNB Chain RPC URL
- Wallet address and private key for Opinion.trade

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd trade_bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project root:
```env
BOT_TOKEN=your_telegram_bot_token
MASTER_KEY=your_32_byte_hex_encryption_key
RPC_URL=your_bnb_chain_rpc_url
ADMIN_TELEGRAM_ID=your_telegram_user_id
PROXY=host:port:username:password  # Optional
```

4. Generate a master key for encryption:
```python
import secrets
print(secrets.token_hex(32))
```

5. Run the bot:
```bash
cd bot
python main.py
```

## Usage

### Registration

1. Start the bot with `/start`
2. Enter your Balance spot address from your [Opinion.trade profile](https://app.opinion.trade/profile)
3. Enter your private key
4. Enter your Opinion Labs API key

All data is encrypted and stored securely.

### Placing Orders

1. Use `/make_market` to start the order placement flow
2. Enter a market URL from Opinion.trade (e.g., `https://app.opinion.trade/detail?topicId=155`)
3. For categorical markets, select a submarket
4. Review market information (spread, liquidity, best bids/asks)
5. Enter the farming amount in USDT
6. Select side (YES or NO)
7. View top 5 bids and asks
8. Set price offset in cents relative to best bid
9. Choose direction (BUY or SELL)
10. Confirm and place the order

## Project Structure

```
bot/
├── main.py          # Main bot logic and handlers
├── config.py        # Configuration and settings
├── aes.py           # AES-GCM encryption utilities
└── users.db         # SQLite database (created automatically)
```

## Security

- **AES-GCM Encryption**: Industry-standard encryption for sensitive data
- **Local Storage**: All data stored locally on your server
- **No Third-Party Sharing**: Your credentials are never shared with third parties
- **Encrypted Database**: SQLite database contains only encrypted data

## Configuration

The bot supports the following environment variables:

- `BOT_TOKEN`: Telegram bot token (required)
- `MASTER_KEY`: 32-byte hex key for encryption (required)
- `RPC_URL`: BNB Chain RPC endpoint (required)
- `ADMIN_TELEGRAM_ID`: Telegram user ID for admin commands (optional)
- `PROXY`: Proxy configuration in format `host:port:username:password` (optional)

## Commands

- `/start` - Register and set up your account
- `/make_market` - Start placing a limit order
- `/get_db` - Export user database (admin only)

## Upcoming Features

🚧 **Coming Soon:**
- **Automatic Order Re-placement**: Orders will be automatically re-placed at the specified offset as market conditions change
- **Order Management**: View, cancel, and manage active orders directly through the bot

## Dependencies

- `aiogram` - Telegram Bot API framework
- `opinion-clob-sdk` - Opinion.trade SDK for market interactions
- `cryptography` - AES-GCM encryption
- `pydantic` - Settings management
- `python-dotenv` - Environment variable loading

## Disclaimer

This bot is provided as-is for educational and personal use. Always ensure you understand the risks involved in trading on prediction markets. The developers are not responsible for any financial losses.

## Support

For issues, questions, or contributions, please open an issue on GitHub.

