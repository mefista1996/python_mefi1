# You are going to write a program that tests the compatibility between two people.
#
# To work out the love score between two people:
#
# Take both people's names and check for the number of times the letters in the word TRUE occurs.
#
# Then check for the number of times the letters in the word LOVE occurs.
#
# Then combine these numbers to make a 2 digit number.
#
# For Love Scores less than 10 or greater than 90, the message should be:
#
# "Your score is **x**, you go together like coke and mentos."
# For Love Scores between 40 and 50, the message should be:
#
# "Your score is **y**, you are alright together."
# Otherwise, the message will just be their score. e.g.:
#
# "Your score is **z**."

print("Welcome to love calculator")
name1 = input("What is your name?")
name2 = input("What is their name?")
name1 = name1.lower()
name2 = name2.lower()

tru_e = name1+name2
w1 = tru_e.count("t")
w1 = tru_e.count("r") + w1
w1 = tru_e.count("u") + w1
w_final = tru_e.count("e") + w1

w2 = tru_e.count("l")
w2 = tru_e.count("o") + w2
w2 = tru_e.count("v") + w2
w2_final = tru_e.count("e") + w2
score = int((str(w_final) + str(w2_final)))

if score <= 10 or score >= 90:
    print(f"Your score is {score}, you go together like coke and mentos.")
elif score >= 40 and score <= 50:
    print(f"Your score is {score}, you are alright together.")
else:
    print(f"Your love score is {score}.")