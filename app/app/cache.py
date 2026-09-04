from requests_cache import CachedSession

from requests_cache import CachedSession
from datetime import timedelta
import os
import logging
from threading import Lock
import redis
from app.metrics import instrument_cached_provider_session

logger = logging.getLogger('cache')
_redis_client = None
_redis_client_lock = Lock()


def setup_fs_cache():
    session_xref = CachedSession(
        'xrefCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=365),
        allowable_methods=['GET'],
        stale_if_error=True,
    )
    session_scpus = instrument_cached_provider_session(CachedSession(
        'scpusCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    ), "scopus")
    
    session_orcid = CachedSession(
        'orcid_session',
       	backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=7),
        stale_if_error=True,
    )
    session_doi = CachedSession(
        'session_doi',
       	backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=7),
        stale_if_error=True,
    )        
        

    return session_xref, session_scpus, session_orcid, session_doi


def setup_redis_cache(redis_host, redis_port):
    logger.info(f"setting up redis {redis_host} {redis_port}")
    session_xref = CachedSession(
        'xrefCache',
        backend='redis',
        host=redis_host,
        port=redis_port,
        expire_after=timedelta(days=365),
        allowable_methods=['GET'],
        stale_if_error=True,
    )
    session_scpus = instrument_cached_provider_session(CachedSession(
        'scpusCache',
        host=redis_host,
        port=redis_port,
        backend='redis',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    ), "scopus")
    
    
    session_orcid = CachedSession(
        'orcid_session',
        host=redis_host,
        port=redis_port,
        backend='redis',
        use_cache_dir=True,
        expire_after=timedelta(days=7),
        stale_if_error=True,
    )
	
    session_doi = CachedSession(
        'session_doi',
        host=redis_host,
        port=redis_port,
        backend='redis',
        use_cache_dir=True,
        expire_after=timedelta(days=7),
        stale_if_error=True,
    )
    
    
    

    return session_xref, session_scpus, session_orcid, session_doi


def get_redis_client():
    global _redis_client
    with _redis_client_lock:
        if _redis_client is False:
            return None
        if _redis_client is not None:
            return _redis_client
        redis_url = os.environ.get("REDIS_URL", "").strip()
        if not redis_url:
            _redis_client = False
            return None
        try:
            redis_host, redis_port = redis_url.split(":")
            client = redis.Redis(host=redis_host, port=int(redis_port), decode_responses=False)
            client.ping()
            _redis_client = client
            logger.info("connected shared redis client host=%s port=%s", redis_host, redis_port)
            return _redis_client
        except Exception:
            logger.exception("failed to initialize shared redis client REDIS_URL=%r", redis_url)
            _redis_client = False
            return None


REDIS_URL = os.environ.get("REDIS_URL", "")

cache_initialized=False

if not cache_initialized:

	try:
		if REDIS_URL != "":
			redis_host, redis_port = REDIS_URL.split(":")
			session_xref, session_scpus, session_orcid,session_doi = setup_redis_cache(redis_host, redis_port)
			logger.info("using redis cache")
		else:
			session_xref, session_scpus, session_orcid,session_doi = setup_fs_cache()
			logger.info("using rs cache")
	except:
		session_xref, session_scpus, session_orcid,session_doi = setup_fs_cache()
		logger.info("using rs cache")
    
	cache_initialized=True
