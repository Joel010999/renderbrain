# runtime/infrastructure/apify — Apify provider adapter package
# Only this package may import apify-client. All other RenderBrain
# components must remain fully decoupled from the provider SDK.
from runtime.infrastructure.apify.adapter import ApifyInstagramAdapter

__all__ = ["ApifyInstagramAdapter"]
