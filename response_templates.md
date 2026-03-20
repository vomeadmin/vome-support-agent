# Vome Support — Response Templates

Standard response templates derived from Zoho Desk snippets.
Used by the support agent when drafting replies to common scenarios.

Placeholders use `{{variable}}` syntax. Replace before sending.

---

## SIGNATURE

```
Best,

Sam | Vome team
support.vomevolunteer.com
```

---

## 1. Register (New Volunteer Registration)

**Use when:** A volunteer contacts support asking how to sign up / apply to volunteer opportunities.

**Template:**

> Thank you for contacting us! In order to apply to volunteer opportunities, you must register and create a profile on Vome.
>
> If you are on your computer, you can register using this link: https://www.vomevolunteer.com/register-volunteer
>
> Otherwise, you can also download the **Vome Volunteer** mobile app as well.
>
> If you are using iPhone: https://apps.apple.com/ca/app/vome-volunteer/id1490871417
>
> If you are using Android: https://play.google.com/store/apps/details?id=com.vome.vomevolunteer
>
> Please let me know if I can assist you with anything else!

---

## 2. VolReg (Volunteer Registration — Troubleshooting)

**Use when:** A volunteer has trouble registering and we are investigating the issue.

**Template:**

> Hi {{name}},
>
> Thank you for contacting us! We will look into the issue to see if we can identify the problem. In the meantime, here are a few other methods to sign up:
>
> If you are on your computer, you can register using this link: https://www.vomevolunteer.com/register-volunteer
>
> Otherwise, you can also download the **Vome Volunteer** mobile app as well.
>
> If you are using iPhone: https://apps.apple.com/ca/app/vome-volunteer/id1490871417
>
> If you are using Android: https://play.google.com/store/apps/details?id=com.vome.vomevolunteer
>
> Please let me know if I can assist you with anything else!

---

## 3. Auth (Authentication Bypass)

**Use when:** We have bypassed email authentication for a user's account.

**Template:**

> Hi {{name}},
>
> I just went ahead and bypassed the authentication for your account. You should now be able to login!
>
> Let me know if there is anything else I can assist you with.

---

## 4. VolGreyButtonReg (Grey Registration Button — Validation Error)

**Use when:** A volunteer reports the registration button is greyed out / not working.

**Template:**

> Thank you for contacting us! Please make sure that you properly added a **phone number** (most common mistake), valid email address, and that your passwords match!
>
> If you are on your computer, you can sign up using this link: https://www.vomevolunteer.com/register-volunteer
>
> Otherwise, you can also download the **Vome Volunteer** mobile app as well.
>
> If you are using iPhone: https://apps.apple.com/ca/app/vome-volunteer/id1490871417
>
> If you are using Android: https://play.google.com/store/apps/details?id=com.vome.vomevolunteer
>
> Please let me know if you are still facing trouble with signing up and I'll be sure to help you get it sorted out!

---

## 5. ForgotPassword (Password Reset — Active Account)

**Use when:** A user says they can't log in but already has an active account.

**Template:**

> Hi {{name}}, thanks for letting us know.
>
> I can confirm that there is already an active account with your email. Therefore, I would expect the password being entered to login is incorrect, or you may be using the wrong website!
>
> Please make sure you are logging in from here: https://www.vomevolunteer.com/login
>
> If you are not able to login successfully, you can try resetting your password using this link: https://www.vomevolunteer.com/forgot
>
> Let me know if you are still continuing to face any issues :)

---

## 6. QuestionsAboutSpecs (Gathering Device/Browser Info)

**Use when:** We need to gather technical details from a user to troubleshoot an issue.

**Template:**

> 1. Are you using your laptop or the downloaded mobile app from the AppStore?
>
> 2. If you **are not using the mobile app**, which browser are you using (i.e. Safari, Google Chrome, Firefox)?
>
> 3. If you **are using the mobile app**, are you using iOS or Android?

---

## 7. CantLogin (Login Troubleshooting — Password Issue)

**Use when:** A user can't log in, we've confirmed they have an active profile, and we suspect a password issue.

**Template:**

> I can see that you do have an active profile with Vome. This means that the password entered might be incorrect.
>
> I would try to **type in your email address and password**, instead of using autofill.
>
> If it still does not work, please try **resetting your password** here: https://www.vomevolunteer.com/forgot
>
> Let me know if that does the trick!

---

## 8. WrongEmailInvited (Email Mismatch on Invitation)

**Use when:** A volunteer signed up with a different email than the one they were invited with.

**Template:**

> Hi {{name}},
>
> I just checked our backend and noticed that {{volunteer_name}} signed up using a different email address than the one invited.
>
> The email you invited: {{invited_email}}
> The email they signed up with: {{signup_email}}
>
> To allow {{volunteer_name}} to accept the invitation, they will need to create a new profile using the same email address invited. **Alternatively**, if they wish to use the email they signed up with, you will need to create a new profile in your database and invite them using the new email address. Once re-invited, {{volunteer_name}} will simply need to **log out and log back into Vome** to accept the invite.
>
> Let me know if you need me to further clarify :)

---

## 9. ThisWasUpdated (Confirming a Change Was Made)

**Use when:** A user requested a change and it has been completed.

**Template:**

> Hi {{name}},
>
> No problem! This has been updated. Let me know if I can help with anything else.

---

## 10. AlreadyAuthenticated (Email Already Verified)

**Use when:** A user reports authentication issues but we can see they are already active.

**Template:**

> Hi {{name}},
>
> I can see that you were able to authenticate your email as I can see you are now active. Let me know if you need anything else!

---

## 11. ContactAdminEmail (Redirect to Organization Admin)

**Use when:** A volunteer contacts Vome support with questions that should be directed to the organization they volunteer for.

**Template:**

> Hi {{name}},
>
> Thanks for the email. Just to let you know, Vome is a technology company that helps organizations streamline their volunteer management operations! So if you have specific questions about registration or reserving shifts, you will need to contact the organization directly.
>
> Let me know if you need help getting in touch with someone (and from which organization!).

---

## 12. PaymentFailed (Subscription Payment Failure Follow-Up)

**Use when:** An organization's subscription payment has failed and we need them to update billing.

**Template:**

> Hi {{name}}!
>
> I am following up since your subscription was set for renewal on {{renewal_date}}, but the payment did not go through.
>
> If needed, you can update the payment method on file by navigating to the **Subscriptions page** > **Change billing information.**
>
> Let me know once that is done from your end or if I should retry the card currently on file.
>
> Please let me know if you have any questions!!

---

## 13. ForgotPasswordWorking (Forgot Password — Confirmed Working)

**Use when:** A user says forgot password isn't working, but we've tested it and it works on our end.

**Template:**

> Hi {{name}},
>
> I can see that your account should be active.
>
> I just checked and tried to resend the password using your email, and it worked successfully for me.
>
> Please double check the spelling of your email to ensure it is entered correctly.
>
> https://www.vomevolunteer.com/forgot
>
> Let me know if you still face any issues.

---

## 14. VolunteerReg (No Account Found — Direct to Signup)

**Use when:** A volunteer contacts us but we can't find an active account with their email.

**Template:**

> Hi {{name}}!
>
> I just checked and I cannot find any active account with your email. To signup, please use this link and follow the steps: https://www.vomevolunteer.com/register-volunteer
>
> Please let me know if you are still running into any issues :)

---

## 15. DeleteUser (Confirm Profile Deletion)

**Use when:** A user has requested account deletion and it has been completed.

**Template:**

> Hi {{name}},
>
> Not a problem! We have just gone ahead and deleted your profile.
>
> Let us know if there is anything else we can do.

---

## 16. FixedIssue-Check (Bug Fix Confirmation — Ask User to Verify)

**Use when:** A reported issue has been fixed and we want the user to confirm it's working.

**Template:**

> I'm happy to report that things should be working as expected. Can you give this another shot and confirm from your end?
>
> Let us know if you need anything else :)

---

## 17. PromptDelete (Deletion Confirmation Warning)

**Use when:** A user asks to delete their profile — we need to warn them before proceeding.

**Template:**

> Hi,
>
> I just wanted to confirm that by deleting your profile, you will also be deleting any shift reservations and hours logged associated with your profile. **This action cannot be undone.**
>
> Please confirm that you would like us to delete your profile.

---

## 18. CannotFindOrg (Volunteer Can't Find Organization)

**Use when:** A volunteer says they can't find a specific organization on Vome.

**Template:**

> Hi {{name}},
>
> Thank you for contacting Vome! I just checked and it seems that {{org_name}} has not yet onboarded onto our platform.
>
> I would suggest reaching out to {{org_name}} directly to ask about how they manage their volunteer registrations. If they do use Vome, they can send you a direct invitation link!
>
> Let me know if you need help with anything else.

---

## 19. HoursNotShowing (Logged Hours Not Appearing)

**Use when:** A volunteer says their logged hours are not showing up.

**Template:**

> Hi {{name}},
>
> Thank you for reaching out. I can see that you do have hours logged in the system.
>
> If your hours are not appearing, please try the following:
> 1. Make sure you are logged into the correct account
> 2. Pull down to refresh the page
> 3. Check that you are looking at the correct organization
>
> If the issue persists, could you send me a screenshot of what you are seeing?

---

## 20. InviteNotReceived (Volunteer Didn't Receive Invitation)

**Use when:** A volunteer says they didn't receive an invitation email from an organization.

**Template:**

> Hi {{name}},
>
> Please check your spam/junk folder as the invitation email may have landed there.
>
> If you still can't find it, please make sure you have an active Vome account registered with the same email address the invitation was sent to. You can register here: https://www.vomevolunteer.com/register-volunteer
>
> Once registered (or if you already have an account), simply **log out and log back in** — the invitation should appear automatically.
>
> Let me know if you're still having trouble!
