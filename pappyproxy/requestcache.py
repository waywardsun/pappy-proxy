import time

import pappyproxy
from .sortedcollection import SortedCollection
from twisted.internet import defer

class RequestCache(object):
    """
    An interface for loading requests. Stores a number of requests in memory and
    leaves the rest on disk. Transparently handles loading requests from disk.
    Most useful functions are :func:`pappyproxy.requestcache.RequestCache.get` to
    get a request by id and :func:`pappyproxy.requestcache.RequestCache.req_id`
    to iterate over requests starting with the most recent requests.

    :ivar cache_size: The number of requests to keep in memory at any given time. This is the number of requests, so if all of the requests are to download something huge, this could still take up a lot of memory.
    :type cache_size: int
    """

    _next_in_mem_id = 1
    _preload_limit = 10
    all_ids = set()
    unmangled_ids = set()
    ordered_ids = SortedCollection(key=lambda x: -RequestCache.req_times[x])
    inmem_reqs = set()
    req_times = {}

    def __init__(self, cache_size=100):
        self._cache_size = cache_size
        if cache_size >= 100:
            RequestCache._preload_limit = int(cache_size * 0.30)
        self._cached_reqs = {}
        self._last_used = {}
        self._min_time = None
        self.hits = 0
        self.misses = 0

    @property
    def hit_ratio(self):
        if self.hits == 0 and self.misses == 0:
            return 0
        return float(self.hits)/float(self.hits + self.misses)

    @staticmethod
    def get_memid():
        i = 'm%d' % RequestCache._next_in_mem_id
        RequestCache._next_in_mem_id += 1
        return i

    def _update_meta(self):
        # Can probably do better to prevent unmangled IDs from being added, but whatever
        over = self._cached_reqs.items()[:]
        for k, v in over:
            if v.unmangled:
                RequestCache.unmangled_ids.add(v.unmangled.reqid)
    
    @staticmethod
    @defer.inlineCallbacks
    def load_ids():
        rows = yield pappyproxy.http.dbpool.runQuery(
            """
            SELECT id, start_datetime FROM requests;
            """
            )
        for row in rows:
            if row[1]:
                RequestCache.req_times[str(row[0])] = row[1]
            else:
                RequestCache.req_times[str(row[0])] = 0
            if str(row[0]) not in RequestCache.all_ids:
                RequestCache.ordered_ids.insert(str(row[0]))
            RequestCache.all_ids.add(str(row[0]))

        rows = yield pappyproxy.http.dbpool.runQuery(
            """
            SELECT unmangled_id FROM requests
            WHERE unmangled_id is NOT NULL;
            """
        )
        for row in rows:
            RequestCache.unmangled_ids.add(str(row[0]))

    def resize(self, size):
        if size >= self._cache_size or size == -1:
            self._cache_size = size
        else:
            while len(self._cached_reqs) > size:
                self._evict_single()
            self._cache_size = size
            
    def assert_ids(self):
        for k, v in self._cached_reqs.iteritems():
            assert v.reqid is not None
        
    @defer.inlineCallbacks
    def get(self, reqid):
        """
        Get a request by id
        """
        self.assert_ids()
        if self.check(reqid):
            self._update_last_used(reqid)
            self.hits += 1
            req = self._cached_reqs[reqid]
            defer.returnValue(req)
        else:
            self.misses += 1
            newreq = yield pappyproxy.http.Request.load_request(reqid, use_cache=False)
            self.add(newreq)
            defer.returnValue(newreq)

    def check(self, reqid):
        """
        Returns True if the id is cached, false otherwise
        """
        self.assert_ids()
        return reqid in self._cached_reqs

    def add(self, req):
        """
        Add a request to the cache
        """
        self.assert_ids()
        if not req.reqid:
            req.reqid = RequestCache.get_memid()
        if req.reqid[0] == 'm':
            self.inmem_reqs.add(req)
        self._cached_reqs[req.reqid] = req
        self._update_last_used(req.reqid)
        RequestCache.req_times[req.reqid] = req.sort_time
        if req.reqid not in RequestCache.all_ids:
            RequestCache.ordered_ids.insert(req.reqid)
        RequestCache.all_ids.add(req.reqid)
        self._update_meta()
        if len(self._cached_reqs) > self._cache_size and self._cache_size != -1:
            self._evict_single()

    def evict(self, reqid):
        """
        Remove a request from the cache by its id.
        """
        # Remove request from cache
        if reqid in self._cached_reqs:

            # Remove id from data structures
            del self._cached_reqs[reqid]
            del self._last_used[reqid]

            # New minimum
            self._update_min(reqid)

    @defer.inlineCallbacks
    def load(self, first, num):
        """
        Load a number of requests after an id into the cache
        """
        reqs = yield pappyproxy.http.Request.load_requests_by_time(first, num)
        for r in reqs:
            self.add(r)
        # Bulk loading is faster, so let's just say that loading 10 requests is
        # 5 misses. We don't count hits since we'll probably hit them
        self.misses += len(reqs)/2.0

    def req_it(self, num=-1, ids=None, include_unmangled=False):
        """
        A generator over all the requests in history when the function was called.
        Generates deferreds which resolve to requests.
        """
        count = 0
        @defer.inlineCallbacks
        def def_wrapper(reqid, load=False, num=1):
            if not self.check(reqid) and load:
                yield self.load(reqid, num)
            req = yield self.get(reqid)
            defer.returnValue(req)
        
        over = list(RequestCache.ordered_ids)
        for reqid in over:
            if ids is not None and reqid not in ids:
                continue
            if not include_unmangled and reqid in RequestCache.unmangled_ids:
                continue
            do_load = True
            if reqid in RequestCache.all_ids:
                if count % RequestCache._preload_limit == 0:
                    do_load = True
                if do_load and not self.check(reqid):
                    do_load = False
                    if (num - count) < RequestCache._preload_limit and num != -1:
                        loadnum = num - count
                    else:
                        loadnum = RequestCache._preload_limit
                    yield def_wrapper(reqid, load=True, num=loadnum)
                else:
                    yield def_wrapper(reqid)
                count += 1
                if count >= num and num != -1:
                    break

    @defer.inlineCallbacks
    def load_by_tag(tag):
        reqs = yield load_requests_by_tag(tag)
        for req in reqs:
            self.add(req)
        defer.returnValue(reqs)

    def _evict_single(self):
        """
        Evicts one item from the cache
        """
        # Get the request
        victim_id = self._min_time[0]
        req = self._cached_reqs[victim_id]
        self.evict(victim_id)

    def _update_min(self, updated_reqid=None):
        new_min = None
        if updated_reqid is None or self._min_time is None or self._min_time[0] == updated_reqid:
            for k, v in self._last_used.iteritems():
                if new_min is None or v < new_min[1]:
                    new_min = (k, v)
            self._min_time = new_min

    def _update_last_used(self, reqid):
        t = time.time()
        self._last_used[reqid] = t
        self._update_min(reqid)

class RequestCacheIterator(object):
    """
    An iterator to iterate over requests in history through the request cache.
    """
    pass