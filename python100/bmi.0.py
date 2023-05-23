# 🚨 Don't change the code below 👇
height = input("enter your height in m: ")
weight = input("enter your weight in kg: ")
# 🚨 Don't change the code above 👆
# Under 18.5 they are underweight
# Over 18.5 but below 25 they have a normal weight
# Over 25 but below 30 they are slightly overweight
# Over 30 but below 35 they are obese
# Above 35 they are clinically obese.

#Write your code below this line 👇
bmi = (round(int(weight)/(float(height)*float(height))))
if bmi < 18.5:
    print(f"You BMI is {bmi}, you are underweight.")
elif bmi > 18.5 and bmi < 25:
    print(f"You BMI is {bmi}, you have a normal weight.")
elif bmi > 25 and bmi < 30:
    print(f"You BMI is {bmi}, you are slightly overweight.")
elif bmi > 30 and bmi < 35:
    print(f"You BMI is {bmi}, you are obese.")
elif bmi > 35 and bmi < 40:
    print(f"You BMI is {bmi}, you are clinically obese.")
else:
    print("How are you even alive!")