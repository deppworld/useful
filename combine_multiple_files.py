import pandas as pd

# Load each file
baseline = pd.read_csv("baseline.csv")
eight_weeks = pd.read_csv("8weeks.csv")
six_months = pd.read_csv("6months.csv")

###### If group or timepoint spacefic column ID is present  and the header is the same in all the files, then combine all files by row###### 

# Load each file (they already have 'Time_Point' column)
baseline = pd.read_csv("baseline.csv")
eight_weeks = pd.read_csv("8weeks.csv")
six_months = pd.read_csv("6months.csv")

# Concatenate without adding extra columns
combined = pd.concat([baseline, eight_weeks, six_months], ignore_index=True)

# Save to CSV
combined.to_csv("combined_metabolites1.csv", index=False)


###Combining file by adding a clouman with keyword(group/timepoint) here Baseline, 8 weeks and 6 months######## 


# Add a column indicating the time point
baseline["Time_Point"] = "Baseline"
eight_weeks["Time_Point"] = "8 Weeks"
six_months["Time_Point"] = "6 Months"

# Combine all into one dataframe
combined = pd.concat([baseline, eight_weeks, six_months], ignore_index=True)

# Save to CSV
combined.to_csv("combined_metabolites_with_extraCloumn.csv", index=False)
