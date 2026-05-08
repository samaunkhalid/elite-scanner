"""
alpaca_news.py
--------------
Strict catalyst / news intelligence for Elite Scanner.

Goal:
- Avoid false catalysts from broad market/news roundup articles.
- Only show catalyst if the headline is company-specific.
- Generic market mover articles are ignored unless the ticker/company appears in headline.

Uses Alpaca News API:
https://data.alpaca.markets/v1beta1/news
"""

import os
import re
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

    # ==========================================================
    # COMPANY-SPECIFIC VALIDATION
    # ==========================================================

    def normalize_text(self, text):
        text = (text or "").lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def company_keywords(self, company_name):
        """
        Extract useful company tokens.
        Avoid generic legal suffixes and weak words.
        """
        name = self.normalize_text(company_name)

        stop_words = {
            "inc", "incorporated", "corp", "corporation", "co", "company",
            "ltd", "limited", "plc", "holdings", "holding", "group",
            "class", "common", "stock", "ordinary", "shares", "the",
            "technologies", "technology", "systems", "solutions"
        }

        words = [w for w in name.split() if len(w) >= 4 and w not in stop_words]

        # Keep only first few meaningful tokens.
        return words[:4]

    def is_generic_market_headline(self, headline):
        """
        Generic articles should not count as company catalyst.
        """
        h = self.normalize_text(headline)

        generic_phrases = [
            "stock market today",
            "stocks moving",
            "big stocks moving",
            "why stocks are moving",
            "market movers",
            "premarket movers",
            "midday movers",
            "after hours movers",
            "top gainers",
            "top losers",
            "nasdaq tops",
            "dow jones",
            "s p 500",
            "wall street",
            "markets rise",
            "markets fall",
            "stocks higher",
            "stocks lower",
            "what s going on with",
        ]

        return any(p in h for p in generic_phrases)

    def is_company_specific(self, symbol, company_name, headline, article):
        """
        Strict test:
        - ticker appears in headline, OR
        - company keyword appears in headline, OR
        - article only has one symbol and belongs to this symbol.
        """
        symbol = (symbol or "").upper().strip()
        h_raw = headline or ""
        h = self.normalize_text(h_raw)

        article_symbols = article.get("symbols", []) or []
        article_symbols = [str(s).upper() for s in article_symbols]

        # 1. Ticker appears as standalone token in headline
        if symbol and re.search(rf"\b{re.escape(symbol.lower())}\b", h):
            return True

        # 2. Company name keywords appear in headline
        keywords = self.company_keywords(company_name)

        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", h):
                return True

        # 3. Single-symbol article can be accepted
        if len(article_symbols) == 1 and symbol in article_symbols:
            return True

        return False

    # ==========================================================
    # HEADLINE CLASSIFIER
    # ==========================================================

    def classify_headline(self, headline):
        """
        Keyword-based classifier.
        Only run this after company-specific validation.
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
            "blowout quarter": 10,
            "strong quarter": 8,
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
        best_reason = "Company-specific fresh news"
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

    def empty_catalyst(self):
        return {
            "catalyst_label": "No confirmed company-specific catalyst",
            "catalyst_sentiment": "NONE",
            "catalyst_score": 0,
            "catalyst_headline": "",
            "catalyst_source": "",
            "catalyst_time": "",
            "risk_flags": "",
        }

    def analyze_symbol_news(self, symbol, company_name, articles):
        """
        Pick the best valid company-specific article.
        Generic market articles are ignored.
        """
        if not articles:
            return self.empty_catalyst()

        valid_articles = []

        for article in articles[:15]:
            headline = article.get("headline", "") or ""

            if not headline:
                continue

            # Reject generic broad market roundup unless company-specific.
            company_specific = self.is_company_specific(
                symbol=symbol,
                company_name=company_name,
                headline=headline,
                article=article,
            )

            if not company_specific:
                continue

            # Even if company-specific, classify it.
            classification = self.classify_headline(headline)

            valid_articles.append({
                "article": article,
                "classification": classification,
                "abs_score": abs(classification["score"]),
            })

        if not valid_articles:
            return self.empty_catalyst()

        # Prefer stronger classified catalyst, otherwise most recent company-specific news.
        valid_articles = sorted(
            valid_articles,
            key=lambda x: x["abs_score"],
            reverse=True,
        )

        best = valid_articles[0]["article"]
        classification = valid_articles[0]["classification"]

        headline = best.get("headline", "") or ""
        source = best.get("source", "") or "Alpaca News"
        created_at = best.get("created_at", "") or best.get("updated_at", "") or ""

        score = classification["score"]
        sentiment = classification["sentiment"]

        if score == 0:
            label = "Company-specific fresh news"
            sentiment = "NEUTRAL"
        else:
            label = classification["label"]

        return {
            "catalyst_label": label,
            "catalyst_sentiment": sentiment,
            "catalyst_score": score,
            "catalyst_headline": headline,
            "catalyst_source": source,
            "catalyst_time": created_at,
            "risk_flags": ", ".join(classification["risk_flags"]),
        }

    def enrich_stocks_with_news(self, stocks, lookback_hours=24):
        """
        Add strict catalyst fields to each stock dict.
        """
        symbols = [s["symbol"] for s in stocks if "symbol" in s]
        news_map = self.fetch_news(symbols, lookback_hours=lookback_hours)

        enriched = 0
        rejected_generic = 0

        for stock in stocks:
            symbol = stock.get("symbol")
            company_name = stock.get("company_name", "") or stock.get("shortName", "") or symbol

            articles = news_map.get(symbol, [])
            catalyst = self.analyze_symbol_news(symbol, company_name, articles)

            if articles and catalyst["catalyst_sentiment"] == "NONE":
                rejected_generic += 1

            stock.update(catalyst)

            if catalyst["catalyst_sentiment"] != "NONE":
                enriched += 1

            # Add catalyst tag only if validated company-specific.
            if catalyst["catalyst_sentiment"] == "POSITIVE":
                stock["tags"] = f"🟢 {catalyst['catalyst_label']} · " + stock.get("tags", "")
            elif catalyst["catalyst_sentiment"] == "NEGATIVE":
                stock["tags"] = f"🔴 {catalyst['catalyst_label']} · " + stock.get("tags", "")
            elif catalyst["catalyst_sentiment"] == "NEUTRAL":
                stock["tags"] = "📰 Company-specific news · " + stock.get("tags", "")

        print(f"  ✓ News-enriched {enriched} stocks with strict company-specific catalysts")
        print(f"  ✓ Rejected {rejected_generic} generic/broad news matches")

        return stocks
