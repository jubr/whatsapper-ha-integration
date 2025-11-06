"""Whatsapper platform for notify component."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html.parser import HTMLParser

import voluptuous as vol

import requests

from homeassistant.components.notify import (
    PLATFORM_SCHEMA,
    BaseNotificationService,
    ATTR_DATA,
    ATTR_TITLE,
    ATTR_MESSAGE,
    ATTR_TARGET,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType


_LOGGER = logging.getLogger(__name__)

HOST_PORT = "host_port"
CONF_CHAT_ID = "chat_id"
ATTR_IMAGE = "image"
ATTR_IMAGE_TYPE = "image_type"
ATTR_IMAGE_NAME = "image_name"

# Cache configuration
CACHE_DURATION = timedelta(minutes=15)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({vol.Required(CONF_CHAT_ID): vol.Coerce(str)})


class ChatListParser(HTMLParser):
    """HTML parser to extract chat information from <li> elements.
    
    Parses format: "Chat Name: chat_id@domain"
    Example: "Purple Tentacle: 31612345678@c.us"
    Handles chat names containing colons.
    """

    def __init__(self):
        super().__init__()
        self.chats = {}

    def handle_starttag(self, tag, attrs):
        """Handle the start of HTML tags."""
        # We'll process in handle_data, nothing to do here
        pass

    def handle_data(self, data):
        """Parse chat data from <li> text content.
        
        Expected format: "Chat Name: chat_id"
        Split on last colon to handle names with colons.
        """
        content = data.strip()
        if not content:
            return
        
        # Split on the last colon to separate name from ID
        # This allows chat names to contain colons
        if ": " in content:
            # Find the last occurrence of ": "
            last_colon_idx = content.rfind(": ")
            chat_name = content[:last_colon_idx].strip()
            chat_id = content[last_colon_idx + 2:].strip()
            
            # Validate chat_id format (should contain @ symbol)
            if chat_id and "@" in chat_id and chat_name:
                self.chats[chat_id] = chat_name
                _LOGGER.debug("Parsed chat: '%s' -> %s", chat_name, chat_id)
        else:
            _LOGGER.debug("Skipping invalid chat format: %s", content)

    def handle_endtag(self, tag):
        """Handle the end of HTML tags."""
        # Nothing to do for end tags
        pass


def get_service(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> WhatsapperNotificationService:
    """Get the Whatsapper notification service."""

    chat_id = config.get(CONF_CHAT_ID)
    host_port = config.get(HOST_PORT)

    if host_port is None:
        host_port = "localhost:4000"

    return WhatsapperNotificationService(hass, chat_id, host_port)


class WhatsapperNotificationService(BaseNotificationService):
    """Whatsapper notification service with chat target discovery."""

    def __init__(self, hass, chat_id, host_port):
        """Initialize the service."""
        self.chat_id = chat_id
        self.host_port = host_port
        self.hass = hass
        self._cached_targets = None
        self._cache_timestamp = None

    @property
    def targets(self):
        """Return a dictionary of registered chat targets.
        
        This property is called by Home Assistant to discover available
        notification targets. Returns a dict mapping chat_id -> chat_name.
        """
        now = datetime.now()
        
        # Return cached targets if still valid
        if (
            self._cached_targets is not None
            and self._cache_timestamp is not None
            and (now - self._cache_timestamp) < CACHE_DURATION
        ):
            return self._cached_targets

        # Fetch and parse chat list
        try:
            url = f'http://{self.host_port}/chats'
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            # Parse HTML to extract chat list
            parser = ChatListParser()
            parser.feed(response.text)
            
            self._cached_targets = parser.chats
            self._cache_timestamp = now
            
            _LOGGER.debug(
                "Fetched %d chat targets from %s",
                len(self._cached_targets),
                url
            )
            
            return self._cached_targets

        except requests.RequestException as e:
            _LOGGER.error("Failed to fetch chat list from %s: %s", url, e)
            # Return cached targets if available, even if expired
            if self._cached_targets is not None:
                _LOGGER.warning("Using expired cache due to fetch failure")
                return self._cached_targets
            # Return empty dict if no cache available
            return {}
        except Exception as e:
            _LOGGER.error("Error parsing chat list: %s", e)
            return self._cached_targets or {}

    def refresh_targets(self):
        """Force refresh of chat targets by invalidating cache."""
        self._cache_timestamp = None
        self._cached_targets = None
        _LOGGER.info("Chat targets cache invalidated")

    def send_message(self, message="", **kwargs):
        """Send a message to the target."""
        try:
            # Use override from notify or the one in the config
            chat_id = kwargs.get(ATTR_TARGET)
            if not chat_id:
                chat_id = self.chat_id
            
            # If target is a list, use the first one
            if isinstance(chat_id, list):
                if not chat_id:
                    _LOGGER.error("Empty target list provided")
                    return
                chat_id = chat_id[0]
            
            data = kwargs.get(ATTR_DATA)

            # Send image if all required image data is present
            if data and all(attr in data for attr in [ATTR_IMAGE, ATTR_IMAGE_TYPE, ATTR_IMAGE_NAME]):
                url = f'http://{self.host_port}/command/media'
                body = {
                    "params": [
                        chat_id,
                        data[ATTR_IMAGE_TYPE],
                        data[ATTR_IMAGE],
                        data[ATTR_IMAGE_NAME]
                    ]
                }
                response = requests.post(url, json=body, timeout=30)
                response.raise_for_status()
                _LOGGER.debug("Sent media message to %s", chat_id)
                return

            # Send text message
            title = kwargs.get(ATTR_TITLE)
            msg = f"{title}\n\n{message}" if title else message
            msg = msg.replace("\\n", "\n")
            
            url = f'http://{self.host_port}/command'
            body = {"command": "sendMessage", "params": [chat_id, msg]}
            response = requests.post(url, json=body, timeout=30)
            response.raise_for_status()
            _LOGGER.debug("Sent text message to %s", chat_id)

        except requests.RequestException as e:
            _LOGGER.error("HTTP request to %s failed: %s", chat_id, e)
        except Exception as e:
            _LOGGER.error("Sending to %s failed: %s", chat_id, e)
