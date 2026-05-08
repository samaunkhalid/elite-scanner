"""
alpaca_news.py
--------------
Catalyst / news intelligence for Elite Scanner.

Uses Alpaca News API:
https://data.alpaca.markets/v1beta1/news

Adds:
- catalyst_label
- catalyst_sentiment
- catalyst_score
- catalyst_headline
- catalyst_source
- catalyst_time
- risk_flags
"""

import os
import requests
from datetime import datetime, timedelta, timezone


class AlpacaNews:
    def __init__(self):
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.base_url = "https://data.alpaca.markets/v1beta1/news"

        self.headers = {
            "APCA-API-KEY-ID": self.api_key or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
        }

    def _chunks(self, symbols, size=25):
        for i in range(0, len(symbols), size):
            yield symbols[i:i + size]

    def fetch_news(self, symbols, lookback_hours=24):
        """
        Fetch recent news for symbols.
        Returns dict: symbol -> list of articles.
        """
        if not self.api_key or not self.secret_key:
            print("  ⚠️ Alpaca News credentials missing")
            return {}

        symbols = [s for s in symbols if isinstance(s, str) and s.strip()]
        symbols = list(dict.fromkeys(symbols))

        start_dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        start = start_dt.isoformat().replace("+00:00", "Z")

        news_by_symbol = {s: [] for s in symbols}

        for batch in self._chunks(symbols, 25):
            params = {
                "symbols": ",".join(batch),
                "start": start,
                "sort": "desc",
                "limit": 50,
                "include_content": "false",
                "exclude_contentless": "true",
            }

            try:
                r = requests.get(
                    self.base_url,
                    headers=self.headers,
                    params=params,
                    timeout=20,
                )

                if r.status_code != 200:
                    print(f"  ⚠️ Alpaca News error {r.status_code}: {r.text[:300]}")
                    continue

                data = r.json()
                articles = data.get("news", []) or []

                for article in articles:
                    article_symbols = article.get("symbols", []) or []
                    for sym in article_symbols:
                        if sym in news_by_symbol:
                            news_by_symbol[sym].append(article)

            except Exception as e:
                print(f"  ⚠️ Alpaca News request failed: {e}")

        return news_by_symbol

    def classify_headline(self, headline):
        """
        Simple keyword-based day-trading catalyst classifier.
        Conservative by design.
        """
        h = (headline or "").lower()

        positive_keywords = {
            "upgrade": 10,
            "upgraded": 10,
            "raises price target": 9,
            "price target raised": 9,
            "raises guidance": 12,
            "guidance raised": 12,
            "beats": 10,
            "beat estimates": 10,
            "better than expected": 8,
            "record revenue": 7,
            "contract": 8,
            "wins contract": 10,
            "partnership": 5,
            "approval": 10,
            "fda approval": 12,
            "acquisition": 12,
            "merger": 12,
            "buyout": 15,
        }

        negative_keywords = {
            "downgrade": -10,
            "downgraded": -10,
            "cuts price target": -8,
            "price target cut": -8,
            "cuts guidance": -12,
            "guidance cut": -12,
            "misses": -10,
            "missed estimates": -10,
            "worse than expected": -8,
            "offering": -15,
            "registered direct": -15,
            "atm offering": -15,
            "shelf": -12,
            "dilution": -15,
            "warrant": -10,
            "investigation": -12,
            "lawsuit": -8,
            "bankruptcy": -15,
            "delisting": -15,
            "reverse split": -12,
            "going concern": -15,
        }

        best_score = 0
        best_reason = "No confirmed catalyst"
        risk_flags = []

        for kw, score in positive_keywords.items():
            if kw in h and score > best_score:
                best_score = score
                best_reason = f"Positive catalyst: {kw}"

        for kw, score in negative_keywords.items():
            if kw in h:
                risk_flags.append(kw)
                if score < best_score:
                    best_score = score
                    best_reason = f"Risk catalyst: {kw}"

        if best_score > 0:
            sentiment = "POSITIVE"
        elif best_score < 0:
            sentiment = "NEGATIVE"
        else:
            sentiment = "NEUTRAL"

        return {
            "score": best_score,
            "label": best_reason,
            "sentiment": sentiment,
            "risk_flags": risk_flags,
        }

    def analyze_symbol_news(self, symbol, articles):
        """
        Pick the most relevant article for a symbol.
        """
        if not articles:
            return {
                "catalyst_label": "No confirmed fresh news",
                "catalyst_sentiment": "NONE",
                "catalyst_score": 0,
                "catalyst_headline": "",
                "catalyst_source": "",
                "catalyst_time": "",
                "risk_flags": "",
            }

        best = None
        best_abs_score = -1
        best_classification = None

        for article in articles[:10]:
            headline = article.get("headline", "") or ""
            classification = self.classify_headline(headline)
            abs_score = abs(classification["score"])

            if abs_score > best_abs_score:
                best = article
                best_abs_score = abs_score
                best_classification = classification

        if best is None:
            return {
                "catalyst_label": "No confirmed fresh news",
                "catalyst_sentiment": "NONE",
                "catalyst_score": 0,
                "catalyst_headline": "",
                "catalyst_source": "",
                "catalyst_time": "",
                "risk_flags": "",
            }

        headline = best.get("headline", "") or ""
        source = best.get("source", "") or "Alpaca News"
        created_at = best.get("created_at", "") or best.get("updated_at", "") or ""

        classification = best_classification or self.classify_headline(headline)

        # If article exists but no keyword hit, label as fresh news but neutral.
        if classification["score"] == 0:
            label = "Fresh news — neutral/unclassified"
            sentiment = "NEUTRAL"
        else:
            label = classification["label"]
            sentiment = classification["sentiment"]

        return {
            "catalyst_label": label,
            "catalyst_sentiment": sentiment,
            "catalyst_score": classification["score"],
            "catalyst_headline": headline,
            "catalyst_source": source,
            "catalyst_time": created_at,
            "risk_flags": ", ".join(classification["risk_flags"]),
        }

    def enrich_stocks_with_news(self, stocks, lookback_hours=24):
        """
        Add catalyst fields to each stock dict.
        """
        symbols = [s["symbol"] for s in stocks if "symbol" in s]
        news_map = self.fetch_news(symbols, lookback_hours=lookback_hours)

        enriched = 0

        for stock in stocks:
            symbol = stock.get("symbol")
            articles = news_map.get(symbol, [])
            catalyst = self.analyze_symbol_news(symbol, articles)

            stock.update(catalyst)

            if catalyst["catalyst_sentiment"] != "NONE":
                enriched += 1

            # Add catalyst tag into visible tags.
            if catalyst["catalyst_sentiment"] == "POSITIVE":
                stock["tags"] = f"🟢 {catalyst['catalyst_label']} · " + stock.get("tags", "")
            elif catalyst["catalyst_sentiment"] == "NEGATIVE":
                stock["tags"] = f"🔴 {catalyst['catalyst_label']} · " + stock.get("tags", "")
            elif catalyst["catalyst_sentiment"] == "NEUTRAL":
                stock["tags"] = "📰 Fresh news · " + stock.get("tags", "")

        print(f"  ✓ News-enriched {enriched} stocks with Alpaca News")
        return stocks
