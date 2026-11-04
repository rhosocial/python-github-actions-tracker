/*
 * Baidu Tongji (百度统计) for the documentation site.
 *
 * The site ID is the organisation's, shared with the other rhosocial sites. Baidu reports
 * per site ID, not per hostname, so a new domain needs its own ID: when this site gets one,
 * replace the value in `hm.src` below -- this is the only place it appears.
 *
 * The script is inserted with `async` so that a blocked or slow hm.baidu.com cannot hold up
 * the page. Readers outside mainland China frequently cannot reach it at all, which is why
 * the analytics must never be on the critical path.
 */
var _hmt = _hmt || [];
(function () {
  var hm = document.createElement("script");
  hm.async = true;
  hm.src = "https://hm.baidu.com/hm.js?94120eca87dedf57cf0c46979991365b";
  var first = document.getElementsByTagName("script")[0];
  first.parentNode.insertBefore(hm, first);
})();
