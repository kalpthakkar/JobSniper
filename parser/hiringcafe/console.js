(async () => {
  /********************************************************************
   * 🔧 INLINE HELPERS (no imports needed)
   ********************************************************************/
  const JOB_KEY_MAP = {
    TITLE: "title",
    COMPANY_NAME: "company",
    APPLY_URL: "applyUrl",
    MATCH_SCORE: "matchScore",
    PUBLISH_TIME_ISO: "publishTime",
    LOCATIONS: "locations",
    SENIORITY: "seniority",
    EMPLOYMENT_TYPE: "employmentType",
    WORK_MODAL: "workplaceType",
    SUMMARY: "summary",
    SKILLS: "skills",
    IS_REPOSTED: "isReposted",
    IS_VISA_SPONSOR: "isVisaSponsor",
    IS_CITIZEN_ONLY: "isCitizenOnly",
    IS_CLEARANCE_REQUIRED: "isClearanceRequired",
    IS_WORK_AUTH_REQUIRED: "isWorkAuthRequired",
    IS_REMOTE: "isRemote",
    IS_DELETED: "isDeleted",
    MIN_SALARY: "minSalary",
    MAX_SALARY: "maxSalary",
    COMPANY_URL: "companyUrl",
    SOURCE_ID: "sourceId",
    APPLICATION_STATUS: "applicationStatus",
    EXECUTION_RESULT: "executionResult",
    APPLIED_AT: "appliedAt"
  };

  const JOBBOARD = "hiringcafe_";

  function matchATS(url, atsList) {
    if (!atsList || !atsList.length) return true;
    return atsList.some(a => url?.toLowerCase().includes(a.toLowerCase()));
  }

  function matchPreference(value, pref) {
    if (pref == null) return true;
    return value === pref;
  }

  function parsePublishTimeUTC(dateStr) {
    if (!dateStr) return null;
    return new Date(dateStr).toISOString();
  }

  function buildDefaultSearchState() {
    return {
      locations: [],
      workplaceTypes: ["Remote", "Hybrid", "Onsite"],
      defaultToUserLocation: false,
      userLocation: null,
      physicalEnvironments: ["Office", "Outdoor", "Vehicle", "Industrial", "Customer-Facing"],
      physicalLaborIntensity: ["Low", "Medium", "High"],
      physicalPositions: ["Sitting", "Standing"],
      oralCommunicationLevels: ["Low", "Medium", "High"],
      computerUsageLevels: ["Low", "Medium", "High"],
      cognitiveDemandLevels: ["Low", "Medium", "High"],
      currency: { label: "Any", value: null },
      frequency: { label: "Any", value: null },
      roleTypes: ["Individual Contributor", "People Manager"],
      securityClearances: ["None", "Confidential", "Public Trust", "Other"],
      hideJobTypes: ["Applied"],
      dateFetchedPastNDays: -1,
      searchQuery: "",
      seniorityLevel: [],
      roleYoeRange: [0, 20],
      managementYoeRange: [0, 20],
      sortBy: "default",
      associatesDegreeFieldsOfStudy: [],
      bachelorsDegreeFieldsOfStudy: [],
      mastersDegreeFieldsOfStudy: [],
      doctorateDegreeFieldsOfStudy: [],
      languagesRequirements: [],
      companyNames: [],
      industries: []
    };
  }

  function syncToDefaultSearchState(CONFIG) {
    const searchState = buildDefaultSearchState();

    searchState.searchQuery = CONFIG.search ?? "";
    searchState.dateFetchedPastNDays =
      CONFIG.publishedWithinHours
        ? Math.ceil(CONFIG.publishedWithinHours / 24)
        : -1;

    if (Array.isArray(CONFIG.locations)) {
      searchState.locations = CONFIG.locations;
    }

    searchState.seniorityLevel = CONFIG.seniority || [];
    searchState.roleYoeRange = CONFIG.roleYoeRange || [0, 20];
    searchState.managementYoeRange = CONFIG.managementYoeRange || [0, 20];

    if (CONFIG.isVisaSponsor === true) {
      searchState.benefitsAndPerks = ["visa_sponsorship"];
    }

    return searchState;
  }

  /********************************************************************
   * 🚀 MAIN FUNCTION
   ********************************************************************/
  async function fetchAllJobs(CONFIG) {
    CONFIG.maxJobs ??= 50;
    if (CONFIG.maxJobs === 0) return [];

    const MAX_PAGE = 50;
    const PAGE_SIZE = 40;

    const searchState = syncToDefaultSearchState(CONFIG);
    const s = encodeURIComponent(
      btoa(unescape(encodeURIComponent(JSON.stringify(searchState))))
    );

    const allJobs = [];
    const seenIds = new Set();

    for (let page = 0; page < MAX_PAGE; page++) {
      let res;
      try {
        res = await fetch(
          `https://hiring.cafe/api/search-jobs?s=${s}&size=${PAGE_SIZE}&page=${page}`,
          { credentials: "include" }
        );
      } catch {
        console.warn("Network error");
        break;
      }

      if (!res.ok) break;

      const json = await res.json();
      const pageJobs = json?.results || [];
      if (!pageJobs.length) break;

      for (const job of pageJobs) {
        if (allJobs.length >= CONFIG.maxJobs) break;
        if (seenIds.has(job.objectID)) continue;
        seenIds.add(job.objectID);

        const jobData = job.v5_processed_job_data || {};
        const companyData = job.v5_processed_company_data || {};

        const atsMatch = matchATS(job.apply_url, CONFIG.ats);

        let timeMatch = true;
        const publishTimeUTC = parsePublishTimeUTC(jobData?.estimated_publish_date);
        if (CONFIG.publishedWithinHours && publishTimeUTC) {
          const diffHrs = (Date.now() - new Date(publishTimeUTC)) / 36e5;
          timeMatch = diffHrs <= CONFIG.publishedWithinHours;
        }

        const preferenceMatch =
          matchPreference(jobData.workplace_type === "Remote", CONFIG.isRemote) &&
          matchPreference(!!jobData.visa_sponsorship, CONFIG.isVisaSponsor);

        if (!(atsMatch && timeMatch && preferenceMatch)) continue;

        allJobs.push({
          title: job.job_information?.title,
          company: companyData.name || jobData.company_name,
          applyUrl: job.apply_url,
          location: jobData?.formatted_workplace_location,
          remote: jobData.workplace_type,
          salaryMin: jobData.yearly_min_compensation,
          salaryMax: jobData.yearly_max_compensation
        });
      }

      console.log(`Page ${page + 1} → total ${allJobs.length}`);
      if (allJobs.length >= CONFIG.maxJobs) break;
    }

    return allJobs;
  }



  /********************************************************************
   * 📄 CSV EXPORT
   ********************************************************************/
  function toCSV(data) {
    if (!data.length) return "";

    const headers = Object.keys(data[0]);

    const escape = (val) =>
      `"${String(val ?? "").replace(/"/g, '""')}"`;

    const rows = data.map(row =>
      headers.map(h => escape(row[h])).join(",")
    );

    return [headers.join(","), ...rows].join("\n");
  }

  function downloadCSV(csv, filename = "jobs.csv") {
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();

    URL.revokeObjectURL(url);
  }

  /********************************************************************
   * ▶️ RUN
   ********************************************************************/
  const jobs = await fetchAllJobs({
    search: "software engineer",
    isRemote: false,
    publishedWithinHours: 1440,
    maxJobs: 50000
  });

  console.log("FINAL JOBS:", jobs);

  const csv = toCSV(jobs);
  downloadCSV(csv, "hiringcafe_jobs.csv");

    
})();