Read CONTEXT.md and system_prompt.md first.

Feature Triage Migration — Phase 2 Execute.

Use these confirmed List IDs for all task creation:
- Master Queue / Priority Queue: 901113386257
- Feature Requests / Raw Intake: 901113386484
- Feature Requests / Accepted Backlog: 901113389889
- Feature Requests / Sleeping: 901113389897
- Feature Requests / Declined: 901113389900

Space ID for VOME Operations: 90114113004

Using the ClickUp MCP, create all tasks listed 
below in VOME Operations according to these 
decisions. This is a two-part migration:
Part A — 64 tasks from main triage
Part B — 28 recovered tasks from testing containers

For every task migrated:
- Create new task in the correct list
- Populate all fields specified below
- Set Source custom field: Migration
- Set Status as specified per task
- Copy original description if noted
- Do NOT delete original tasks in VOMEDev —
  we will archive that space separately
- Print progress as you go:
  "Created task X: [title]"
- If a task fails, log the error and 
  continue with the next task — 
  do not stop the entire migration

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART A — MAIN MIGRATION (64 tasks)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MASTER QUEUE — PRIORITY QUEUE
List ID: 901113386257
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

P1 BUGS
Status: QUEUED
Auto Score: 75
Assignee: Sam

1. Checklist step > Hitting Save and popup 
   not closing + note field only appearing 
   after Save
   Type: Bug | Module: Sequences | Platform: Web

2. Form Submission Expanded > Vol already 
   in DB > View profile opens wrong popup
   Type: Bug | Module: Forms | Platform: Web

3. Opp DB > Update Application deadline 
   date > Not showing as saved until 
   hard refresh
   Type: Bug | Module: Opportunities | Platform: Web

4. Auto Populated Vome fields > Include 
   filter logic not working
   Type: Bug | Module: Forms | Platform: Web

5. Vol user seeing linked SITE under 
   My Organizations not My Sites
   Type: Bug | Module: Sites | Platform: Web

6. Database > Custom view > Not applying 
   Sites filter as expected
   Type: Bug | Module: Admin Dashboard | Platform: Web

7. Opportunity Dashboard > People > 
   Assign Profiles > Show Default 
   database view
   Type: Bug | Module: Opportunities | Platform: Web

8. Vol assigned to opportunity > Not 
   receiving in-system notification
   Type: Bug | Module: Opportunities | Platform: Web
   Tags: Notification

9. Manage notification policies > Same 
   title as Vome notification causes 
   silent save failure
   Type: Bug | Module: Email Communications | Platform: Web

10. SSO email collision: admin + vol same 
    email via different auth methods
    Type: Bug | Module: Access / Authentication
    Platform: Both | Auto Score: 70

IN PROGRESS BUGS
Status: IN PROGRESS
Auto Score: 80

11. Vol/Admin > FR User > Visit support 
    links > bring to French support page URLs
    Type: Bug | Module: Access / Authentication
    Platform: Web | Assignee: Sam

12. Admin in-system notifications in FR > 
    Accents displaying in weird characters
    Type: Bug | Module: Email Communications
    Platform: Web | Assignee: Sam

13. Shift Almost Over and Upcoming Shift 
    Reminder still going through when 
    setting is off
    Type: Bug | Module: Reserve Schedule
    Platform: Web | Assignee: Sam
    Tags: Notification

QA BUGS
Status: QUEUED
Auto Score: 65

14. Push notification > Fix Youve text error
    Type: Bug | Module: Reserve Schedule | Platform: Mobile

15. Groups module > Permissions update
    Type: Feature | Module: Groups | Platform: Web
    Status: IN PROGRESS | Assignee: Sam

16. Auto-populated Vome field questions > 
    Logic update
    Type: Improvement | Module: Forms | Platform: Web
    Status: IN PROGRESS | Assignee: Sam

17. Vol Manage Guests > After guest spot 
    claimed > Lead cant see Remove/Edit
    Type: Bug | Module: Groups | Platform: Web

18. Need to allow admin to add user with 
    Pending/Declined status to waitlist
    Type: Bug | Module: Reserve Schedule | Platform: Web
    Status: IN PROGRESS | Assignee: Sam

19. Admin notifications > Replace weird 
    characters with French accents
    Type: Bug | Module: Email Communications | Platform: Web

20. Pending shift requests > Show on past 
    shifts and remove notification
    Type: Bug | Module: Reserve Schedule | Platform: Web
    Tags: Notification

21. Update Notification JSON Files on Prod
    Type: Improvement | Module: Email Communications
    Platform: Web | Status: IN PROGRESS | Assignee: Sam

QUICK FIXES
Status: QUEUED
Auto Score: 50

22. Update text in Notifications JSON file
    Type: Improvement | Module: Email Communications
    Platform: Web

23. Integrations and Apps page > Add 
    E-Learning section for SCORM
    Type: Improvement | Module: Integrations | Platform: Web

24. Landing Page > Add new logos
    Type: Improvement | Module: Other | Platform: Web

25. Landing page update to mention 
    new integrations
    Type: Improvement | Module: Other | Platform: Web

26. Invite admin popup error message > 
    Update text
    Type: Improvement | Module: Admin Settings | Platform: Web

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE REQUESTS — RAW INTAKE
List ID: 901113386484
Status: QUEUED
Auto Score: 60 unless noted
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

27. MTL Childrens Hospital > Feature 
    requests For Q1 2026
    Type: Feature | Module: Sequences | Platform: Web
    Requesting Clients: Montreal Childrens Hospital
    Auto Score: 65
    Sprint Batch: MTL Childrens Hospital Q1 2026
    Note: Create as parent task with these 5 subtasks:
    - Flexible Scheduling for Long-Term Shifts
    - More User-Friendly Check-In Kiosk
    - Bulk recurring shift booking by volunteers
    - Sequence step visibility toggle for admin
    - Sequence step notification filtering

28. Wellspring Alberta Updates
    Type: Feature | Module: Sequences | Platform: Web
    Requesting Clients: Wellspring Alberta
    Auto Score: 65
    Note: Create as parent task with 13 subtasks —
    fetch and copy all subtask titles from 
    original task in Feature Triage space

29. Vol profile > Background check button > 
    Sterling Volunteers
    Type: Feature | Module: Integrations | Platform: Web

30. Mark as absent > updates
    Type: Feature | Module: Admin Scheduling | Platform: Web

31. Allow vol to upload new version of 
    fillable PDF upon expiration date
    Type: Feature | Module: Sequences | Platform: Web

32. Time off feature: auto-cancelling 
    reservations in that period
    Type: Feature | Module: Reserve Schedule | Platform: Web

33. HTML format for sequence steps
    Type: Feature | Module: Sequences | Platform: Web

34. Report > Time off report
    Type: Feature | Module: Reports | Platform: Web

35. Sequence > Custom step > Display 
    expiration date setting by default
    Type: UX | Module: Sequences | Platform: Web

36. Group Reservation Policy minor tweaks
    Type: Feature | Module: Groups | Platform: Web
    Requesting Clients: Welcome Hall Mission
    Note: Create as parent task with 4 subtasks —
    fetch and copy all subtask titles from 
    original task in Feature Triage space

37. Shifts and hours by profile report > 
    Need to make Sites a Data Filter
    Type: Feature | Module: Reports | Platform: Web

38. DB and Forms > Sequences field > show 
    current step number and filter by step
    Type: Feature | Module: Forms | Platform: Web

39. Auto-populated Vome field question > 
    Include private opps > Add 
    semi-private logic
    Type: Feature | Module: Forms | Platform: Web

40. Automated anniversary emails based 
    on Start Date field
    Type: Feature | Module: Email Communications | Platform: Web

41. When publishing opportunity > Notify 
    only eligible users
    Type: Feature | Module: Opportunities | Platform: Web
    Tags: Notification

42. Vol Reserve shifts > Add Calendar view 
    Weekly Monthly
    Type: Feature | Module: Reserve Schedule | Platform: Web

43. Forms and Database > Add Advanced 
    filter for Sequence Status
    Type: Feature | Module: Forms | Platform: Web

44. Admin Mobile App > Priority Updates
    Type: Feature | Module: Admin Dashboard | Platform: Mobile

45. Database > Archived profiles > Bulk 
    Actions > Assign/Unassign sites
    Type: Feature | Module: Admin Dashboard | Platform: Web

46. Sequence step settings > Add 
    Auto-unassign profile tags
    Type: Feature | Module: Sequences | Platform: Web

47. Sequence Dashboard report > Need 
    export view with DB fields
    Type: Feature | Module: Reports | Platform: Web

48. Analytics module > Data needs to be 
    segmented by Site or Role access
    Type: Feature | Module: KPI Dashboards | Platform: Web

49. Track Years of Service/Anniversary
    Type: Feature | Module: Admin Dashboard | Platform: Web
    Tags: Notification

50. Database > Make default Name field = 
    Separate Name Columns
    Type: UX | Module: Admin Dashboard | Platform: Web

51. Sequence Dashboard > Add Step 
    assignment date field
    Type: Feature | Module: Sequences | Platform: Web

52. When a user deletes their account > 
    Add Deleted status banner
    Type: Feature | Module: Admin Dashboard | Platform: Web

53. Edit shift template > Add 
    Link to Site field
    Type: Feature | Module: Admin Scheduling | Platform: Web

54. Create shifts > Allow admin to choose 
    to display attendee count to vol
    Type: Feature | Module: Admin Scheduling | Platform: Web

55. Allow admin to assign shift even if 
    attendee status is Cancelled
    Type: Improvement | Module: Admin Scheduling | Platform: Web

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE REQUESTS — ACCEPTED BACKLOG
List ID: 901113389889
Status: QUEUED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

56. Sequence step settings update 
    (expiration, deadline, historical 
    file access)
    Type: Feature | Module: Sequences | Platform: Web
    Auto Score: 55
    Note: Has partially specced subtask —
    fetch subtask content and copy into 
    Design Spec field

57. Shift template updates > Add new 
    features available here
    Type: Feature | Module: Admin Scheduling | Platform: Web
    Auto Score: 50
    Note: Create with 2 subtasks:
    - Waitlist policy
    - Notification policy

58. Consider mass updates around 
    separating first and last name
    Type: Improvement | Module: Admin Dashboard | Platform: Web
    Auto Score: 30
    Note: Not high priority per Sam

59. Guest submission wrap up
    Type: Feature | Module: Groups | Platform: Web
    Auto Score: 55
    Note: Decent priority per Sam

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE REQUESTS — SLEEPING
List ID: 901113389897
Status: SLEEPING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

60. Consider new onboarding process > 
    auto generated password
    Type: Feature | Module: Access / Authentication
    Platform: Web
    Wake Date: May 1 2026

61. Landing page revamp
    Type: Feature | Module: Other | Platform: Web
    Wake Date: September 1 2026

62. Custom achievement / goals system / 
    impact tracking
    Type: Feature | Module: Volunteer Homepage | Platform: Web
    Wake Date: September 1 2026

63. Analytics and KPI revamp
    Type: Feature | Module: KPI Dashboards | Platform: Web
    Wake Date: September 1 2026

64. Recruitment workflow
    Type: Feature | Module: Other | Platform: Web
    Wake Date: September 1 2026

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART B — RECOVERED TASKS FROM TESTING CONTAINERS
(28 tasks — all go to Master Queue Priority Queue)
List ID: 901113386257
Status: QUEUED
Source: Migration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FROM: Auto-populated Vome fields testing
Module: Forms | Platform: Web

65. Need to translate backend validation 
    text to French
    Type: Bug | Auto Score: 60
    Tags: Notification

66. Filter/Exclude dropdowns dont work 
    when adding first question of this type
    Type: Bug | Auto Score: 65

67. Update from raw string to real text
    Type: Bug | Auto Score: 50

FROM: Form Submission Table custom views
Module: Forms | Platform: Web

68. Add Sequences as a filter
    Type: Feature | Auto Score: 55

69. Refresh page should not revert to 
    default custom view
    Type: Bug | Auto Score: 70

70. Add Database Status as a filter
    Type: Feature | Auto Score: 55

71. Not found in DB filter showing 
    wrong results
    Type: Bug | Auto Score: 70

FROM: Group Res Admin Manage guests
Module: Groups | Platform: Web

72. Profile picture not displayed on 
    claimed spot
    Type: Bug | Auto Score: 45

73. No guests currently text instead 
    of None
    Type: UX | Auto Score: 35

74. Re-invite frozen screen + email not 
    delivered after email change
    Type: Bug | Auto Score: 80
    Note: Critical bug — frozen screen

75. Show Email under guest name after 
    invite sent
    Type: UX | Auto Score: 45

76. Cannot see Copy link button after 
    shift ends
    Type: Bug | Auto Score: 55

77. Log hours popup should say Log hours 
    not Check-out
    Type: UX | Auto Score: 35

FROM: Group Res Kiosk Testing
Module: Kiosk | Platform: Web

78. Transferred lead role > Previous lead 
    cannot click Send by email button
    Type: Bug | Auto Score: 60

FROM: Import shifts testing
Module: Admin Scheduling | Platform: Web

79. Publishing import doesnt auto-refresh 
    schedule
    Type: Bug | Auto Score: 55

80. Add French import shift template
    Type: Improvement | Auto Score: 45

FROM: Advanced calculation field testing
Module: Forms | Platform: Web

81. Add Copy string or Add to description 
    button
    Type: UX | Auto Score: 60

82. Translate Function section to French
    Type: Improvement | Auto Score: 65

83. Label needs French string Etiquette
    Type: Improvement | Auto Score: 65

84. Translate Summary count of to French
    Type: Improvement | Auto Score: 65

85. Remove underscore from 
    Profile information
    Type: Bug | Auto Score: 45

86. Remove underscore from Date of birth
    Type: Bug | Auto Score: 45

87. Remove underscore from Value source
    Type: Bug | Auto Score: 45

88. Remove underscore from Profile field
    Type: Bug | Auto Score: 45

89. Make Add lowercase
    Type: Bug | Auto Score: 40

90. Nouveau should be on one line
    Type: Bug | Auto Score: 40

FROM: New sequence editor testing
Module: Sequences | Platform: Web

91. Display due date to volunteer
    Type: Feature | Auto Score: 45

92. Display step assignment date 
    to volunteer
    Type: Feature | Auto Score: 35

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DO NOT MIGRATE — SKIP THESE ENTIRELY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The following tasks should NOT be migrated.
Leave them in VOMEDev as-is:

- Form Submission Table > Custom views 
  Testing notes (parent container only)
- Group Res > Admin Manage guests 
  Testing notes (parent container only)
- Group Res > Kiosk Testing 
  (parent container only)
- FAQ > How does Groups module work 
  (documentation, not a product task)
- Shifts and hours by profile report > 
  Add Sites Data filter 
  (duplicate — already migrated as #37)
- Import shifts > Testing notes 
  (parent container only)
- Advanced calculation field > Testing notes 
  (parent container only)
- New sequence editor design > testing notes 
  (parent container only)
- Update Notifications.json file 
  (already closed)
- Opportunity Schedule settings 
  (skip per Sam — no description)
- Add to DB logic when user applies via 
  direct opp funnel 
  (delete per Sam — no longer relevant)
- Form Questions > Auto-populated Vome 
  fields > Testing notes 
  (parent container only)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL SUMMARY REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

After completing all task creation, 
output a final summary showing:

Total tasks created: X
- Master Queue Priority Queue: X
- Feature Requests Raw Intake: X
- Feature Requests Accepted Backlog: X
- Feature Requests Sleeping: X

Tasks skipped: X
Errors encountered: X (list each one)

Any tasks that failed must be listed 
by title so they can be created manually.
