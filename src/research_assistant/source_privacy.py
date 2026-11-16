"""Shared recognized credential/temporary URL rules for connector boundaries."""
import re
CREDENTIAL_KEYS = frozenset({'api_key','api_token','authorization','access_token','refresh_token','password'})
SIGNED_QUERY_KEYS = frozenset({'x_amz_signature','x_amz_credential','x_amz_security_token','signature',
                              'access_token','token','x_goog_signature','api_key'})
URL_PATTERN = re.compile(r'https?://[^\s<>"\'`\)\]]+')
