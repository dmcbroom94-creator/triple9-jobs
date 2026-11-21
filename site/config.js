/* Where the live job list is loaded from. Jobs and courses are loaded from GitHub, which is updated automatically. */
window.T9_JOBS_URL = 'https://raw.githubusercontent.com/dmcbroom94-creator/triple9-jobs/main/jobs.json';
window.T9_COURSES_URL = 'https://raw.githubusercontent.com/dmcbroom94-creator/triple9-jobs/main/courses.json';

(function () {
  /* Format by named parts, not locale-dependent date ordering. */
  function ukDay(now) {
    const parts = new Intl.DateTimeFormat('en-GB-u-ca-gregory-nu-latn', {
      timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(now);
    const get = (type) => parts.find((part) => part.type === type).value;
    const year = get('year').padStart(4, '0');
    const month = get('month').padStart(2, '0');
    const day = get('day').padStart(2, '0');
    if (!/^\d{4}$/.test(year) || !/^\d{2}$/.test(month) || !/^\d{2}$/.test(day)) throw new Error('Invalid UK date');
    return year + '-' + month + '-' + day;
  }

  /* Manual listings are visible through their UK review date. The optional
     Date argument is useful for testing; ordinary listings need no date. */
  window.T9_isJobWithinReview = function (job, now) {
    if (!job || typeof job !== 'object') return false;
    if (!Object.prototype.hasOwnProperty.call(job, 'manualReviewUntil')) return true;

    const value = job.manualReviewUntil;
    const match = typeof value === 'string' && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match) return false;
    const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const daysInMonth = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth[month - 1]) return false;

    try {
      return value >= ukDay(now === undefined ? new Date() : now);
    } catch (e) {
      /* If the current UK date cannot be determined, hide manual listings. */
      return false;
    }
  };

  /* A page left open overnight must also update, including across BST changes. */
  window.T9_scheduleReviewRefresh = function (refresh) {
    if (typeof refresh !== 'function' || typeof window.setTimeout !== 'function') return;
    function schedule() {
      try {
        const now = Date.now(), today = ukDay(new Date(now));
        let low = now, high = now + 26 * 60 * 60 * 1000;
        while (high - low > 1000) {
          const mid = Math.floor((low + high) / 2);
          if (ukDay(new Date(mid)) === today) low = mid;
          else high = mid;
        }
        window.setTimeout(function () { refresh(); schedule(); }, high - now + 1);
      } catch (e) {
        /* The review helper already hides manual jobs when UK dates fail. */
      }
    }
    schedule();
    if (typeof document !== 'undefined' && document.addEventListener) {
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') refresh();
      });
    }
  };
})();
