"""Fuzzy text correction for STT mishearings.

Maps common Whisper misheard words to their intended targets.
Uses difflib for fuzzy matching when exact matches fail.
"""

from __future__ import annotations

import logging
import re
from difflib import get_close_matches

logger = logging.getLogger("blankslate.voice.correction")

# Direct word mappings: misheard → correct
# Based on actual Indian English accent mishearings observed
WORD_MAP: dict[str, str] = {
    # App names
    "void": "word",
    "flood": "word",
    "load": "word",
    "wood": "word",
    "word.": "word",
    "not bad": "notepad",
    "note bad": "notepad",
    "note pad": "notepad",
    "note-pad": "notepad",
    "notepad.": "notepad",
    "crem": "chrome",
    "chrome.": "chrome",
    "breathe": "brave",
    "brave.": "brave",
    "blang": "blank",
    "blanked": "blank",
    "blank.": "blank",
    "blank slate.": "blankslate",
    "blank slate": "blankslate",
    "paint.": "paint",
    "calculator.": "calculator",
    "cursor.": "cursor",
    "explorer.": "explorer",
    "terminal.": "terminal",
    
    # Common command words
    "minimize": "minimize",
    "manimize": "minimize",
    "switch": "switch",
    "swich": "switch",
    "open": "open",
    "close": "close",
    "type": "type",
    
    # App names (additional mishearings)
    "work": "word",
    "brief": "brave",
    "explain": "explorer",
    "explosion": "explorer",
    "experiment": "explorer",
    "needland": "hey blankslate",
    
    # Wake words
    "hey assistant": "hey blankslate",
    "hey blanks": "hey blankslate",
    "hey blanked": "hey blankslate",
    "hey blang": "hey blankslate",
}

# Multi-word phrase corrections
PHRASE_MAP: dict[str, str] = {
    "open not bad": "open notepad",
    "open note bad": "open notepad",
    "open note pad": "open notepad",
    "open the browser": "open chrome",
    "launch the browser": "open chrome",
    "start the browser": "open chrome",
    "open the calculator": "open calculator",
    "start the calculator": "open calculator",
    "launch the calculator": "open calculator",
    "open the paint": "open paint",
    "launch the paint": "open paint",
    "start the paint": "open paint",
    "switch to the chrome": "switch to chrome",
    "minimize the window": "minimize window",
    "minimise window": "minimize window",
    "minimise the window": "minimize window",
}


def correct_text(raw_text: str) -> str:
    """Apply fuzzy correction to STT output.
    
    Steps:
    1. Lowercase for matching
    2. Try exact phrase matches first
    3. Try word-by-word correction with fuzzy matching
    4. Return corrected text (preserving original case where possible)
    """
    if not raw_text:
        return raw_text
    
    text = raw_text.strip()
    text_lower = text.lower()
    
    # 1. Try exact phrase matches
    for phrase, correction in PHRASE_MAP.items():
        if phrase in text_lower:
            # Replace the phrase while preserving case of surrounding text
            text = re.sub(re.escape(phrase), correction, text, flags=re.IGNORECASE)
            text_lower = text.lower()
            logger.debug("Phrase correction: '%s' → '%s'", phrase, correction)
    
    # 2. Word-by-word correction
    words = text.split()
    corrected_words = []
    
    for word in words:
        word_lower = word.lower().strip(".,!?;:")
        word_clean = word.strip(".,!?;:")
        punctuation = ""
        
        # Extract trailing punctuation
        if len(word) > len(word_clean):
            punctuation = word[len(word_clean):]
        
        # Try exact match first
        if word_lower in WORD_MAP:
            corrected_words.append(WORD_MAP[word_lower] + punctuation)
            logger.debug("Exact correction: '%s' → '%s'", word_lower, WORD_MAP[word_lower])
            continue
        
        # Try fuzzy match (similar spelling)
        matches = get_close_matches(word_lower, WORD_MAP.keys(), n=1, cutoff=0.7)
        if matches:
            best_match = matches[0]
            corrected_words.append(WORD_MAP[best_match] + punctuation)
            logger.debug("Fuzzy correction: '%s' → '%s' (matched '%s')", word_lower, WORD_MAP[best_match], best_match)
            continue
        
        # No correction found
        corrected_words.append(word)
    
    corrected = " ".join(corrected_words)
    
    if corrected != raw_text:
        logger.info("STT corrected: '%s' → '%s'", raw_text, corrected)
    
    return corrected


def add_correction(word: str, correction: str) -> None:
    """Dynamically add a word correction mapping."""
    WORD_MAP[word.lower()] = correction.lower()
    logger.info("Added correction: '%s' → '%s'", word, correction)


def add_phrase_correction(phrase: str, correction: str) -> None:
    """Dynamically add a phrase correction mapping."""
    PHRASE_MAP[phrase.lower()] = correction.lower()
    logger.info("Added phrase correction: '%s' → '%s'", phrase, correction)
