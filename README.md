# Opinion.trade Market Making Bot

A Telegram bot for placing limit orders on [Opinion.trade](https://app.opinion.trade) prediction markets. The bot provides an intuitive interface for market making strategies with secure credential management, invite-based access control, and automatic order synchronization.

## Features

### 🔐 Secure Registration with Invite System
- **Invite-Based Access**: Registration requires a valid invite code (10-character alphanumeric)
- **Encrypted Storage**: All sensitive data (wallet address, private key, API key) is encrypted using AES-GCM encryption
- **Async SQLite Database**: User credentials are stored locally in an encrypted SQLite database using `aiosqlite` for non-blocking operations
- **Zero Trust**: Your private keys never leave your server in unencrypted form
- **Atomic Invite Usage**: Invites are used atomically at the end of registration to prevent conflicts

### 🎫 Invite Management (Admin Only)
- **Invite Generation**: Admin command `/get_invites` generates and displays 10 unused invite codes
- **Automatic Creation**: System automatically creates new invites if fewer than 10 are available
- **Statistics**: View total, used, and unused invite counts
- **One-Time Use**: Each invite can only be used once
- **Unique Codes**: 10-character alphanumeric codes with uniqueness validation

### 📊 Market Order Placement
- **Interactive Flow**: Step-by-step process for placing limit orders
- **Market Analysis**: View market information including:
  - Best bid/ask prices for YES and NO tokens
  - Spread and liquidity metrics
  - Top 5 bids and asks with price visualization in cents
- **Smart Validation**: Automatic balance checks and price validation
- **Categorical Markets**: Support for multi-outcome markets with submarket selection
- **Reposition Threshold**: Configurable threshold (in cents) for when orders should be repositioned
- **Error Handling**: Clear error messages when API calls fail

### 💰 Order Management
- **Order List**: View all your orders with pagination (`/orders` command)
- **Order Search**: Search orders by order ID, market ID, market title, token name, or side
- **Order Cancellation**: Cancel orders directly from the bot interface with detailed error messages
- **Price Offset in Cents**: Set order prices relative to best bid using intuitive cent-based offsets
- **Direction Selection**: Choose BUY (below current price) or SELL (above current price)
- **Order Confirmation**: Review all settings before placing orders
- **Order Status Tracking**: View order status (active, filled, cancelled)

### 🔄 Automatic Order Synchronization
- **Background Task**: Automatically synchronizes orders every 60 seconds
- **Order Status Monitoring**: Checks order status via API before processing
  - Automatically updates database when orders are filled or cancelled externally
  - Sends notifications for filled orders with order details (price, market link)
  - Silently updates cancelled orders without notifications
- **Price Tracking**: Monitors market price changes and maintains constant offset from current price
- **Smart Updates**: Only moves orders when price change exceeds configurable threshold (default 0.5 cents)
- **Batch Operations**: Efficiently cancels and places orders in batches per user
- **User Notifications**: Sends notifications about:
  - Price changes (before repositioning)
  - Order updates (after successful repositioning)
  - Order filled (when order is executed)
  - Placement errors (with detailed error messages)
- **Non-blocking**: All operations are asynchronous and don't block the bot's event loop
- **Safety Checks**: Only places new orders after successfully canceling old ones

### 📝 Logging & Monitoring
- **Separate Log Files**: Different log files for different modules:
  - `bot.log` - Main bot operations
  - `sync_orders.log` - Order synchronization operations
- **Detailed Logging**: Comprehensive logging with user IDs, market IDs, and execution times
- **Performance Monitoring**: Logs start time, end time, and duration for each user's processing
- **Error Tracking**: Full traceback logging for debugging

### 💬 Support System
- **User Support**: Command `/support` allows users to contact the administrator
- **Message Forwarding**: Support messages (text or photo with caption) are forwarded to the admin
- **User Information**: Admin receives user ID and username with each support message
- **No Registration Required**: Support command is available to all users (no registration needed)
- **Confirmation**: Users receive confirmation when their message is sent

### 🛡️ Security & Performance
- **Anti-Spam Protection**: Built-in middleware to prevent message spam
- **Async Architecture**: Fully asynchronous codebase using `aiosqlite` and `asyncio`
- **Non-blocking I/O**: All database and API operations are non-blocking
- **Modular Design**: Clean separation of concerns with routers and modules
- **Registration Check**: Commands verify user registration before execution

## Getting Started

### Prerequisites

- Python 3.13+
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Opinion Labs API Key (obtain from [the form](https://docs.google.com/forms/d/1h7gp8UffZeXzYQ-lv4jcou9PoRNOqMAQhyW4IwZDnII/viewform?edit_requested=true))
- BNB Chain RPC URL
- Wallet address and private key for Opinion.trade
- Admin Telegram ID (for invite management)

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
2. Enter your invite code (10-character alphanumeric code)
3. Enter your Balance spot address from your [Opinion.trade profile](https://app.opinion.trade/profile)
4. Enter your private key
5. Enter your Opinion Labs API key

All data is encrypted and stored securely. The invite code is validated and used atomically at the end of registration.

### Invite Management (Admin Only)

1. Use `/get_invites` to get 10 unused invite codes
2. The system automatically creates new invites if needed
3. View statistics: total, used, and unused invite counts
4. Share invite codes with users who need access

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
10. Set reposition threshold (minimum price change in cents to trigger repositioning, default 0.5)
11. Confirm and place the order

### Managing Orders

1. Use `/orders` to view all your orders
2. Browse orders with pagination (10 orders per page)
3. Use the search function to find specific orders
4. Cancel orders by entering the order ID (order list remains visible for easy copying)
5. View order details: status, price, amount, market, creation date

### Contacting Support

1. Use `/support` to contact the administrator
2. Enter your question or describe the issue
3. You can send text or a photo with a caption
4. Your message will be forwarded to the administrator with your user information
5. You'll receive a confirmation when your message is sent

## Project Structure

```
bot/
├── main.py                  # Main bot entry point, background tasks, admin commands
├── config.py                # Configuration and settings management
├── database.py              # Async database operations (aiosqlite)
├── aes.py                   # AES-GCM encryption utilities
├── client_factory.py         # Opinion SDK client creation and proxy setup
├── spam_protection.py       # Anti-spam middleware
├── logger_config.py         # Logging configuration and setup
├── start_router.py          # User registration flow (/start command)
├── market_router.py         # Market order placement flow (/make_market command)
├── orders_dialog.py         # Order management dialog (/orders command)
├── sync_orders.py           # Automatic order synchronization background task
├── opinion_api_wrapper.py   # Opinion API wrapper functions (async)
├── invites.py               # Invite management functions
└── users.db                 # SQLite database (created automatically)
```

## Architecture

The bot uses a modular router-based architecture:

- **Routers**: Separate routers for different features (`start_router`, `market_router`)
- **Async Database**: All database operations use `aiosqlite` for non-blocking I/O
- **Background Tasks**: Order synchronization runs as an independent async task
- **Dialogs**: Complex multi-step interactions use `aiogram-dialog` for better UX
- **Middleware**: Global anti-spam protection for all messages and callbacks
- **API Wrapper**: Centralized async wrapper for Opinion API calls

## Security

- **AES-GCM Encryption**: Industry-standard encryption for sensitive data
- **Local Storage**: All data stored locally on your server
- **No Third-Party Sharing**: Your credentials are never shared with third parties
- **Encrypted Database**: SQLite database contains only encrypted data
- **Async Operations**: Non-blocking I/O prevents performance issues
- **Invite System**: Access control through invite codes

## Configuration

The bot supports the following environment variables:

- `BOT_TOKEN`: Telegram bot token (required)
- `MASTER_KEY`: 32-byte hex key for encryption (required)
- `RPC_URL`: BNB Chain RPC endpoint (required)
- `ADMIN_TELEGRAM_ID`: Telegram user ID for admin commands (required for invite management)
- `PROXY`: Proxy configuration in format `host:port:username:password` (optional)

## Commands

### User Commands
- `/start` - Register and set up your account (requires invite code)
- `/make_market` - Start placing a limit order
- `/orders` - View, search, and manage your orders
- `/support` - Contact administrator with questions or issues (supports text and photos)

### Admin Commands
- `/get_db` - Export user database (admin only)
- `/get_invites` - Get 10 unused invite codes with statistics (admin only)

## Automatic Order Synchronization

The bot automatically synchronizes your orders every 60 seconds:

### How it works:
1. **Status Check**: For each active order, checks status via API
   - If order is filled: updates database, sends notification with order details
   - If order is cancelled: updates database silently (no notification)
   - If status check fails: continues with normal processing

2. **Price Monitoring**: Monitors market prices and maintains a constant offset (in ticks) between the current market price and your order's target price

3. **Smart Updates**: Only moves orders when the price change exceeds the reposition threshold (default 0.5 cents)

4. **Batch Operations**: Efficiently cancels and places orders in batches per user

5. **Notifications**: You'll receive notifications when:
   - Market price changes and orders need to be moved (before repositioning)
   - Orders are successfully updated with new prices (after repositioning)
   - Orders are filled (with order details and market link)
   - Placement errors occur (with detailed error messages)

### Features:
- **Efficiency**: Skips repositioning when change < threshold (saves API calls and gas fees)
- **Reliability**: Only places new orders after successfully canceling old ones
- **Safety**: Validates all operations via API response codes (errno == 0)
- **User Awareness**: Detailed notifications for all important events
- **Performance**: Logs execution time for each user's processing

## Dependencies

- `aiogram==3.23.0` - Telegram Bot API framework
- `aiogram-dialog==2.4.0` - Dialog system for complex interactions
- `aiosqlite==0.22.0` - Async SQLite driver for non-blocking database operations
- `opinion-clob-sdk==0.4.3` - Opinion.trade SDK for market interactions
- `cryptography==46.0.3` - AES-GCM encryption
- `pydantic==2.12.5` - Settings management
- `pydantic-settings==2.12.0` - Environment variable settings
- `python-dotenv==1.2.1` - Environment variable loading

## Technical Details

### Async Architecture
- All database operations use `aiosqlite` for true async I/O
- API calls are wrapped in `asyncio.to_thread()` to prevent blocking
- Background tasks run independently without blocking the main event loop
- Opinion API wrapper provides async interface for synchronous SDK

### Order Synchronization Algorithm
1. Retrieves all users from the database
2. For each user:
   - Gets active orders from the database
   - For each order:
     - **Status Check**: Checks order status via API
       - If filled: updates DB, sends notification, skips processing
       - If cancelled: updates DB, skips processing
     - Fetches current market price (best_bid for BUY, best_ask for SELL)
     - Calculates new target price using saved `offset_ticks`
     - Calculates price change in cents
     - If price change ≥ `reposition_threshold_cents`, adds to cancellation/placement lists
     - Sends price change notification (only if order will be repositioned)
   - Cancels old orders in batch (validates via errno == 0)
   - Places new orders in batch (only if all old orders were cancelled)
   - Updates database with new order parameters (only for successful placements)
   - Sends order update notification (for successful placements)
   - Sends error notification (for failed placements)

### Invite System
- Invites are stored in `invites` table with fields: id, invite (unique), telegram_id, created_at, used_at
- Invite codes are 10-character alphanumeric strings
- Invites are validated before registration and used atomically at the end
- Admin can generate invites via `/get_invites` command
- System automatically creates new invites if needed

### Database Schema
- **users**: Encrypted user credentials (wallet, private key, API key)
- **orders**: Order information (order_id, market_id, prices, amounts, status, etc.)
- **invites**: Invite codes and usage tracking

## Disclaimer

This bot is provided as-is for educational and personal use. Always ensure you understand the risks involved in trading on prediction markets. The developers are not responsible for any financial losses.

## Support

For issues, questions, or contributions, please open an issue on GitHub.
