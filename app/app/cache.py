from requests_cache import CachedSession

from requests_cache import CachedSession
from datetime import timedelta
import os
import logging

logger = logging.getLogger('cache')


def setup_fs_cache():
    session_xref = CachedSession(
        'xrefCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=365),
        allowable_methods=['GET'],
        stale_if_error=True,
    )
    session_scpus = CachedSession(
        'scpusCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    )
    
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
    session_scpus = CachedSession(
        'scpusCache',
        host=redis_host,
        port=redis_port,
        backend='redis',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    )
    
    
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