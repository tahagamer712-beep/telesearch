# 🔍 TeleSearch — Telegram Bot for Local File Search & Indexing

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![SQLite](https://img.shields.io/badge/SQLite-FTS5-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

**Search your local files through Telegram. Fast. Private. Offline.**

[Features](#-features) • [Quick Start](#-quick-start) • [Documentation](#-documentation) • [Contributing](#-contributing)

</div>

---

## 🌟 What is TeleSearch?

TeleSearch is a powerful Telegram bot that indexes your local files (Excel, PDF, SQLite, CSV, JSON, Access databases, and more) and lets you search through millions of records instantly — all from your phone, with **complete privacy**. Your data never leaves your device.

### ✨ Features

- 📁 **Multi-format Support**: Excel (.xlsx/.xls), PDF, SQLite, CSV, JSON, Access (.accdb/.mdb), Word, and archives (ZIP/RAR/7z)
- 🔍 **Lightning-fast Search**: Full-text search with SQLite FTS5 — millions of records in milliseconds
- 🤖 **AI-Powered Smart Search**: Natural language queries via OpenRouter (optional)
- 👥 **Entity Resolution**: Automatically links related records across files
- 📊 **Rich Reports**: Export results as HTML, PDF, or Excel
- 🔐 **Role-based Access**: Admin/User roles with granular permissions
- 📱 **Telegram Interface**: Full bot UI with inline buttons — no commands to memorize
- 🌐 **Bilingual**: Full Arabic and English support
- 💾 **100% Local**: No cloud, no tracking, your data stays yours
- 📦 **Batch Processing**: Background job queue for large files

### 🎯 Use Cases

- 📋 Search customer/patient records
- 📚 Index document archives
- 🏢 Manage inventory databases
- 📞 Phone/address directories
- 🗃️ Any tabular data you need to search on the go

---

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Termux (for Android) or any Linux system

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/telesearch.git
cd telesearch

# Run installer
chmod +x install.sh
./install.sh

# Configure
cp .env.example .env
nano .env  # Add your TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_IDS

# Start the bot
./start.sh
```

### Configuration (.env)

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
AUTHORIZED_USER_IDS=123456789  # Your Telegram user ID (becomes admin)
# Optional: AI-powered smart search
OPENROUTER_API_KEY=your_key_here
OPENROUTER_MODEL=nvidia/nemotron-3-ultra:free
```

---

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [INSTALL.md](docs/INSTALL.md) | Detailed installation guide |
| [SECURITY.md](docs/SECURITY.md) | Security model and best practices |
| [MATCHING.md](docs/MATCHING.md) | Entity resolution explained |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues and fixes |

---

## 🏗️ Architecture

```
Telegram Bot → App (Auth/RBAC/Audit) → {Ingestion, Search, AI} → SQLite (+FTS5)
```

- **Ingestion**: File hashing, classification, format-specific parsers
- **Indexing**: SQLite with FTS5 full-text search, chunked for large files
- **Search**: Fast full-text + advanced filters + AI natural language
- **Security**: Whitelist + roles + audit log for every action

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- Built with [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- Full-text search powered by [SQLite FTS5](https://www.sqlite.org/fts5.html)
- AI search via [OpenRouter](https://openrouter.ai)

---

<div align="center">

**⭐ Star this repo if you find it useful!**

Made with ❤️ for privacy-conscious data search

</div>
